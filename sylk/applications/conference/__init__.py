# Copyright (C) 2010-2011 AG Projects. See LICENSE for details
#

import mimetypes
import os
import re

from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null
from gnutls.interfaces.twisted import X509Credentials
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError, Header, ContactHeader, FromHeader, ToHeader
from sipsimple.lookup import DNSLookup
from sipsimple.streams import AudioStream
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ISylkApplication, SylkApplication
from sylk.applications.conference.configuration import get_room_config, ConferenceConfig
from sylk.applications.conference.room import Room
from sylk.applications.conference.web import ScreenSharingWebServer
from sylk.bonjour import BonjourServices
from sylk.configuration import SIPConfig, ThorNodeConfig
from sylk.extensions import ChatStream
from sylk.session import ServerSession
from sylk.tls import Certificate, PrivateKey

# Initialize database
from sylk.applications.conference import database


class ACLValidationError(Exception): pass

class RoomNotFoundError(Exception): pass


class ConferenceApplication(object):
    __metaclass__ = SylkApplication
    implements(ISylkApplication, IObserver)

    __appname__ = 'conference'

    def __init__(self):
        self._rooms = {}
        self.pending_sessions = []
        self.invited_participants_map = {}
        self.bonjour_services = BonjourServices()
        self.bonjour_services.start()
        self.bonjour_services.activate()
        self.screen_sharing_web_server = ScreenSharingWebServer(ConferenceConfig.screen_sharing_dir)
        if ConferenceConfig.screen_sharing_use_https and ConferenceConfig.screen_sharing_certificate is not None:
            cert = Certificate(ConferenceConfig.screen_sharing_certificate.normalized)
            key = PrivateKey(ConferenceConfig.screen_sharing_certificate.normalized)
            credentials = X509Credentials(cert, key)
        else:
            credentials = None
        self.screen_sharing_web_server.run(ConferenceConfig.screen_sharing_ip, ConferenceConfig.screen_sharing_port, credentials)

    def get_room(self, uri, create=False):
        room_uri = '%s@%s' % (uri.user, uri.host)
        try:
            room = self._rooms[room_uri]
        except KeyError:
            if create:
                room = Room(room_uri)
                self._rooms[room_uri] = room
                return room
            else:
                raise RoomNotFoundError
        else:
            return room

    def remove_room(self, uri):
        room_uri = '%s@%s' % (uri.user, uri.host)
        self._rooms.pop(room_uri, None)

    def validate_acl(self, room_uri, from_uri):
        room_uri = '%s@%s' % (room_uri.user, room_uri.host)
        cfg = get_room_config(room_uri)
        if cfg.access_policy == 'allow,deny':
            if cfg.allow.match(from_uri) and not cfg.deny.match(from_uri):
                return
            raise ACLValidationError
        else:
            if cfg.deny.match(from_uri) and not cfg.allow.match(from_uri):
                raise ACLValidationError

    def incoming_session(self, session):
        log.msg('New incoming session from %s' % session.remote_identity.uri)
        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        transfer_streams = [stream for stream in session.proposed_streams if stream.type=='file-transfer']
        if not audio_streams and not chat_streams and not transfer_streams:
            session.reject(488)
            return
        try:
            self.validate_acl(session._invitation.request_uri, session.remote_identity.uri)
        except ACLValidationError:
            session.reject(403)
            return
        # Check if requested files belong to this room
        for stream in (stream for stream in transfer_streams if stream.direction == 'sendonly'):
            try:
                room = self.get_room(session._invitation.request_uri)
            except RoomNotFoundError:
                session.reject(404)
                return
            try:
                file = (file for file in room.files if file.hash == stream.file_selector.hash).next()
            except StopIteration:
                session.reject(404)
                return
            filename = os.path.basename(file.name)
            for dirpath, dirnames, filenames in os.walk(os.path.join(ConferenceConfig.file_transfer_dir, room.uri)):
                if filename in filenames:
                    path = os.path.join(dirpath, filename)
                    stream.file_selector.fd = open(path, 'r')
                    if stream.file_selector.size is None:
                        stream.file_selector.size = os.fstat(stream.file_selector.fd.fileno()).st_size
                    if stream.file_selector.type is None:
                        mime_type, encoding = mimetypes.guess_type(filename)
                        if encoding is not None:
                            type = 'application/x-%s' % encoding
                        elif mime_type is not None:
                            type = mime_type
                        else:
                            type = 'application/octet-stream'
                        stream.file_selector.type = type
                    break
            else:
                # File got removed from the filesystem
                session.reject(404)
                return
        self.pending_sessions.append(session)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        if audio_streams:
            session.send_ring_indication()
        streams = [streams[0] for streams in (audio_streams, chat_streams, transfer_streams) if streams]
        reactor.callLater(4 if audio_streams else 0, self.accept_session, session, streams)

    def incoming_subscription(self, subscribe_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        if Null in (from_header, to_header):
            subscribe_request.reject(400)
            return
        try:
            self.validate_acl(data.request_uri, from_header.uri)
        except ACLValidationError:
            try:
                self.validate_acl(to_header.uri, from_header.uri)
            except ACLValidationError:
                # Check if we need to skip the ACL because this was an invited participant
                if not (str(from_header.uri) in self.invited_participants_map.get('%s@%s' % (data.request_uri.user, data.request_uri.host), {}) or
                        str(from_header.uri) in self.invited_participants_map.get('%s@%s' % (to_header.uri.user, to_header.uri.host), {})):
                    subscribe_request.reject(403)
                    return
        try:
            room = self.get_room(data.request_uri)
        except RoomNotFoundError:
            try:
                room = self.get_room(to_header.uri)
            except RoomNotFoundError:
                subscribe_request.reject(480)
                return
        if not room.started:
            subscribe_request.reject(480)
        else:
            room.handle_incoming_subscription(subscribe_request, data)

    def incoming_referral(self, refer_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        refer_to_header = data.headers.get('Refer-To', Null)
        if Null in (from_header, to_header, refer_to_header):
            refer_request.reject(400)
            return
        try:
            self.validate_acl(data.request_uri, from_header.uri)
        except ACLValidationError:
            refer_request.reject(403)
            return
        referral_handler = IncomingReferralHandler(refer_request, data)
        referral_handler.start()

    def incoming_sip_message(self, message_request, data):
        message_request.answer(405)

    def accept_session(self, session, streams):
        if session in self.pending_sessions:
            session.accept(streams, is_focus=True)

    def add_participant(self, session, room_uri):
        log.msg('Outgoing session to %s started' % session.remote_identity.uri)
        # Keep track of the invited participants, we must skip ACL policy
        # for SUBSCRIBE requests
        room_uri_str = '%s@%s' % (room_uri.user, room_uri.host)
        d = self.invited_participants_map.setdefault(room_uri_str, {})
        d.setdefault(str(session.remote_identity.uri), 0)
        d[str(session.remote_identity.uri)] += 1
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        room = self.get_room(room_uri, True)
        room.start()
        room.add_session(session)

    def remove_participant(self, participant_uri, room_uri):
        try:
            room = self.get_room(room_uri)
        except RoomNotFoundError:
            pass
        else:
            room.terminate_sessions(participant_uri)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        room = self.get_room(session._invitation.request_uri, True)    # FIXME
        room.start()
        room.add_session(session)

    @run_in_green_thread
    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session from %s ended' % session.remote_identity.uri)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        if session.direction == 'incoming':
            room_uri = session._invitation.request_uri               # FIXME
        else:
            # Clear invited participants mapping
            room_uri_str = '%s@%s' % (session.local_identity.uri.user, session.local_identity.uri.host)
            d = self.invited_participants_map[room_uri_str]
            d[str(session.remote_identity.uri)] -= 1
            if d[str(session.remote_identity.uri)] == 0:
                del d[str(session.remote_identity.uri)]
            room_uri = session.local_identity.uri
        # We could get this notifiction even if we didn't get SIPSessionDidStart
        try:
            room = self.get_room(room_uri)
        except RoomNotFoundError:
            return
        if session in room.sessions:
            room.remove_session(session)
        if not room.stopping and room.empty:
            self.remove_room(room_uri)
            room.stop().wait()

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        log.msg('Session from %s failed' % session.remote_identity.uri)


class IncomingReferralHandler(object):
    implements(IObserver)

    def __init__(self, refer_request, data):
        self._refer_request = refer_request
        self._refer_headers = data.headers
        self.room_uri = data.headers.get('To').uri
        self.refer_to_uri = re.sub('<|>', '', data.headers.get('Refer-To').uri)
        self.method = data.headers.get('Refer-To').parameters.get('method', 'invite').lower()
        self.session = None
        self.streams = []

    def start(self):
        if not self.refer_to_uri.startswith(('sip:', 'sips:')):
            self.refer_to_uri = 'sip:%s' % self.refer_to_uri
        try:
            self.refer_to_uri = SIPURI.parse(self.refer_to_uri)
        except SIPCoreError:
            self._refer_request.reject(488)
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._refer_request)
        if self.method == 'invite':
            log.msg('%s added %s to %s' % (self._refer_headers.get('From').uri, self.refer_to_uri, self.room_uri))
            self._refer_request.accept()
            settings = SIPSimpleSettings()
            account = AccountManager().default_account
            if account.sip.outbound_proxy is not None:
                uri = SIPURI(host=account.sip.outbound_proxy.host,
                             port=account.sip.outbound_proxy.port,
                             parameters={'transport': account.sip.outbound_proxy.transport})
            else:
                uri = self.refer_to_uri
            lookup = DNSLookup()
            notification_center.add_observer(self, sender=lookup)
            lookup.lookup_sip_proxy(uri, settings.sip.transport_list)
        elif self.method == 'bye':
            log.msg('%s removed %s from %s' % (self._refer_headers.get('From').uri, self.refer_to_uri, self.room_uri))
            self._refer_request.accept()
            conference_application = ConferenceApplication()
            conference_application.remove_participant(self.refer_to_uri, self.room_uri)
            self._refer_request.end(200)
        else:
            self._refer_request.reject(488)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        account = AccountManager().default_account
        conference_application = ConferenceApplication()
        try:
            room = conference_application.get_room(self.room_uri)
        except RoomNotFoundError:
            return
        else:
            active_media = room.active_media
        if not active_media:
            return
        if 'audio' in active_media:
            self.streams.append(AudioStream(account))
        if 'chat' in active_media:
            self.streams.append(ChatStream(account))
        self.session = ServerSession(account)
        notification_center.add_observer(self, sender=self.session)
        original_from_header = self._refer_headers.get('From')
        if original_from_header.display_name:
            original_identity = "%s <%s@%s>" % (original_from_header.display_name, original_from_header.uri.user, original_from_header.uri.host)
        else:
            original_identity = "%s@%s" % (original_from_header.uri.user, original_from_header.uri.host)
        from_header = FromHeader(SIPURI.new(self.room_uri), u'Conference Call')
        to_header = ToHeader(self.refer_to_uri)
        transport = notification.data.result[0].transport
        parameters = {} if transport=='udp' else {'transport': transport}
        contact_header = ContactHeader(SIPURI(user=self.room_uri.user, host=SIPConfig.local_ip, port=getattr(SIPConfig, 'local_%s_port' % transport), parameters=parameters))
        extra_headers = []
        if self._refer_headers.get('Referred-By', None) is not None:
            extra_headers.append(Header.new(self._refer_headers.get('Referred-By')))
        else:
            extra_headers.append(Header('Referred-By', str(original_from_header.uri)))
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(original_from_header.uri)))
        subject = u'Join conference request from %s' % original_identity
        self.session.connect(from_header, to_header, contact_header, routes=notification.data.result, streams=self.streams, is_focus=True, subject=subject, extra_headers=extra_headers)

    def _NH_DNSLookupDidFail(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)

    def _NH_SIPSessionGotRingIndication(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(180)

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(notification.data.code, notification.data.reason)

    def _NH_SIPSessionDidStart(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        conference_application = ConferenceApplication()
        conference_application.add_participant(self.session, self.room_uri)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidFail(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(notification.data.code or 500, notification.data.reason)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidEnd(self, notification):
        # If any stream fails to start we won't get SIPSessionDidFail, we'll get here instead
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        self.session = None
        self.streams = []

    def _NH_SIPIncomingReferralDidEnd(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        self._refer_request = None


