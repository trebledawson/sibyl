#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Sibyl: A modular Python chat bot framework
# Copyright (c) 2015-2016 Joshua Haas <jahschwa.com>
#
# JabberBot: A simple jabber/xmpp bot framework
# Copyright (c) 2007-2012 Thomas Perl <thp.io/about>
# $Id: d1c7090edd754ff0da8ef4eb10d4b46883f34b9f $
#
# This file is part of Sibyl.
#
# Sibyl is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
################################################################################

import logging,time,re,traceback

import xmpp
from xmpp.protocol import SystemShutdown,StreamError

from sibyl.lib.protocol import *
from sibyl.lib.decorators import botconf

################################################################################
# Config options                                                               #
################################################################################

@botconf
def conf(bot):
  return [
    {'name':'jid','req':True},
    {'name':'password','req':True},
    {'name':'resource','default':'sibyl'},
    {'name':'server','default':None},
    {'name':'port','default':5222,'parse':bot.conf.parse_int},
    {'name':'ping_freq','default':60,'parse':bot.conf.parse_int},
    {'name':'ping_timeout','default':3,'parse':bot.conf.parse_int},
    {'name':'debug','default':False,'parse':bot.conf.parse_bool},
    {'name':'priv_domain','default':True,'parse':bot.conf.parse_bool}
  ]

################################################################################
# Custom Exceptions                                                            #
################################################################################

class MUCJoinFailure(Exception):
  pass

################################################################################
# JID(User) class                                                              #
################################################################################

class JID(User):

  def parse(self,jid,typ):
    """accept either xmpp.JID for internal use, or str for external use"""

    # format for a JID is: node@domain/resource
    # for private chat this looks like: user@domain/resource
    # for group chat this looks like: room@domain/nick
    
    self.muc = (typ==Message.GROUP)
    if isinstance(jid,xmpp.JID):
      self.jid = jid
    else:
      self.jid = xmpp.JID(jid)

  def get_name(self):
    """return username or nick name"""

    if self.muc:
      return self.jid.getResource()
    return self.jid.getNode()

  def get_room(self):
    """return room name or None"""

    if self.muc:
      return Room(self.jid.getStripped())

  def get_base(self):
    """return JID without resource, or full MUC JID"""

    if self.muc:
      return str(self.jid)
    return self.jid.getStripped()

  def __eq__(self,other):
    """we can just compare our internal xmpp.JID"""

    if not isinstance(other,JID):
      return False
    return self.jid==other.jid

  def __str__(self):
    """return full JID, including resources if we have one"""

    if self.muc:
      return self.get_base()
    if self.jid.getResource():
      return str(self.jid)
    return self.get_base()
  __repr__ = __str__

################################################################################
# XMPP(Protocol) class                                                         #
################################################################################

class XMPP(Protocol):

  # xmpp show types
  AVAILABLE, AWAY, CHAT = None, 'away', 'chat'
  DND, XA, OFFLINE = 'dnd', 'xa', 'unavailable'

  # MUC status
  MUC_PARTED = -2
  MUC_PENDING = -1
  MUC_OK = 0
  MUC_ERR = 1
  MUC_BANNED = 301
  MUC_KICKED = 307
  MUC_AFFIL = 321
  MUC_MEMBERS = 322
  MUC_SHUTDOWN = 332

  # human-readable MUC errors
  MUC_JOIN_ERROR = {'not-authorized'          : 'invalid password',
                    'forbidden'               : 'banned',
                    'item-not-found'          : 'room does not exist',
                    'not-allowed'             : 'room creation forbidden',
                    'not-acceptable'          : 'must use reserved nick',
                    'registration-required'   : 'not a member',
                    'conflict'                : 'nick in use',
                    'service-unavailable'     : 'room is full',
                    'remote-server-not-found' : 'server not found',
                    'timeout'                 : 'timeout'}

  def setup(self):

    # MUC error codes, reason, and log level
    self.MUC_CODES = {self.MUC_PARTED   : ('parted',None),
                      self.MUC_OK       : ('OK',None),
                      self.MUC_ERR      : ('unknown',self.log.error),
                      self.MUC_BANNED   : ('banned',self.log.critical),
                      self.MUC_KICKED   : ('kicked',self.log.error),
                      self.MUC_AFFIL    : ('affiliation',self.log.info),
                      self.MUC_MEMBERS  : ('members-only',self.log.error),
                      self.MUC_SHUTDOWN : ('shutdown',self.log.error)}

    # translate xmpp types to sibyl types
    self.TYPES = {'presence':Message.STATUS,
                  'chat':Message.PRIVATE,
                  'groupchat':Message.GROUP,
                  'error':Message.ERROR}

    # translate xmpp show types to sibyl types
    self.STATUS = {'xa':Message.EXT_AWAY,
                    'away':Message.AWAY,
                    'dnd':Message.DND,
                    'chat':Message.AVAILABLE,
                    None:Message.AVAILABLE}

    self.conn = None
    self.jid = xmpp.JID(self.opt('xmpp.jid'))
    
    self.roster = None
    self.seen = {}
    
    self.mucs = {}
    self.__muc_pending = []
    self.real_jids = {}

    self.last_muc = None
    self.last_ping = time.time()
    self.last_join = self.last_ping

  def connect(self):
    """try to connect if we aren't connected"""

    if self.conn:
      return

    self.log.debug('Attempting to connect with JID "%s"' % self.opt('xmpp.jid'))
    conn = xmpp.Client(self.jid.getDomain(), debug=self.opt('xmpp.debug'))

    # connection attempt
    server = self.opt('xmpp.server') or self.jid.getDomain()
    port = self.opt('xmpp.port')
    conres = conn.connect((server,port))

    if not conres:
      raise ConnectFailure
    if conres != 'tls':
      self.log.warning('unable to establish secure connection '\
      '- TLS failed!')

    # authentication attempt
    authres = conn.auth(self.jid.getNode(),
        self.opt('xmpp.password'),self.opt('xmpp.resource'))
    if not authres:
      raise AuthFailure
    if authres != 'sasl':
      self.log.warning("unable to perform SASL auth on %s. "\
      "Old authentication method used!" % self.jid.getDomain())

    # Connection established - save connection
    self.conn = conn

    # Register handlers
    self.conn.RegisterHandler('message',self.callback_message)
    self.conn.RegisterHandler('presence',self.callback_presence)

    # Send initial presence stanza
    self.conn.sendInitPresence()
    
    # Save roster and log Items
    self.roster = self.conn.Roster.getRoster()
    self.log.info('*** roster ***')
    for contact in self.roster.getItems():
      self.log.info('  %s' % contact)
    self.log.info('*** roster ***')

  def is_connected(self):
    """return True if we are still connected"""

    return (self.conn is not None)

  def disconnected(self):
    """erase self.conn and set all MUCS to parted"""

    self.conn = None
    for muc in self.__get_current_mucs():
      self.mucs[muc]['status'] = self.MUC_PARTED
    self.seen = {}

  def process(self,wait=0):
    """process messages and __idle_proc"""

    try:
      self.conn.Process(wait)
    except SystemShutdown:
      raise ServerShutdown
    except StreamError as e:
      self.log.error(traceback.format_exception_only(type(e),e)[:-1])
      raise ConnectFailure
    
    self.__idle_proc()

  def shutdown(self):
    """we don't have to do anything special here"""

    pass

  def send(self,text,to):
    """send a message to the specified recipient"""

    mess = self.__build_message(text)
    mess.setType('chat')
    if isinstance(to,JID) and to.get_room():
      to = to.get_room()
    if isinstance(to,Room):
      mess.setType('groupchat')
      to = to.get_name()
    mess.setTo(xmpp.JID(str(to)))

    try:
      self.conn.send(mess)
    except IOError:
      raise ConnectFailure

  def broadcast(self,text,room,frm=None):
    """send a message to every user in a room"""

    # XMPP has no built-in broadcast, so we'll just highlight everyone
    s = ''
    me = JID(room.get_name()+'/'+self.get_nick(room),Message.GROUP)
    for user in self.get_occupants(room):
      if user!=me and (not frm or user!=frm):
        s += (user.get_name()+': ')

    text = '%s[ %s ] %s' % (s,text,frm.get_name())
    self.send(text,room)

  def join_room(self,room):
    """join a room and return True if joined successfully or False otherwise"""

    name = room.get_name()
    nick = room.get_nick() or self.opt('nick_name')
    pword = room.get_password()

    # do nothing if we're already trying to join that room
    for x in self.__muc_pending:
      if x[1]==name:
        return

    # we'll receive a reply stanza with matching id telling us if we succeeded,
    # but due to the way xmpppy processes stanzas, we can't easily wait for the
    # given stanza inside a callback without blocking or race conditions
    self.mucs[name] = {'pass':pword,'nick':nick,'status':self.MUC_PENDING}
    self.__muc_pending.append((name,nick,pword))

  def part_room(self,room):
    """leave the specified room"""

    name = room.get_name()

    # build the part stanza
    if self.mucs[name]['status']==self.MUC_OK:
      room_jid = name+'/'+self.mucs[name]['nick']
      pres = xmpp.Presence(to=room_jid)
      pres.setAttr('type', 'unavailable')
      try:
        self.conn.send(pres)
      except IOError:
        raise ConnectFailure

    # update mucs dict and log
    self.mucs[name]['status'] = self.MUC_PARTED
    self.log.debug('Parted room "%s"' % name)

  def in_room(self,room):
    """return True/False if we are in the specified room"""

    return room.get_name() in self.__get_current_mucs()

  def _get_rooms(self,flag):
    """return rooms matching the given flag"""

    if flag==Room.FLAG_PARTED:
      rooms = self.__get_inactive_mucs()
    elif flag==Room.FLAG_PENDING:
      rooms = [room for (room,d) in self.mucs.items()
        if d['status']==self.MUC_PENDING or d['status']>self.MUC_OK]
    elif flag==Room.FLAG_IN:
      rooms = self.__get_current_mucs()
    elif flag==Room.FLAG_ALL:
      rooms = self.mucs.keys()
    else:
      rooms = []

    return [Room(room) for room in rooms]

  def get_occupants(self,room):
    """return the Users in the given room, or None if we are not in the room"""

    name = room.get_name()

    # search through who we've seen for anyone with the room in their JID
    users = []
    for jid in self.seen:
      if ((name==jid.getStripped())
          and (self.mucs[name]['nick']!=jid.getResource())):
        users.append(JID(jid,Message.GROUP))

    me = JID(name+'/'+self.get_nick(room),Message.GROUP)
    if me not in users:
      users.append(me)

    return users

  def get_nick(self,room):
    """return our nick in the given room"""

    return self.mucs[room.get_name()]['nick']

  def get_real(self,room,nick):
    """return the real username of the given nick"""

    if nick==self.get_nick(room):
      return JID(self.jid,Message.PRIVATE)

    jid = xmpp.JID(room.get_name()+'/'+nick)
    real = self.real_jids.get(jid,nick)
    if real==nick:
      return JID(jid,Message.GROUP)

    return JID(real,Message.PRIVATE)

  def get_username(self):
    """return our username"""
    
    return JID(self.jid,Message.PRIVATE)

  def new_user(self,user,typ):
    """create a new user of this Protocol's User class"""

    return JID(user,typ)

################################################################################
# Internal callbacks                                                           #
################################################################################

  def callback_message(self, conn, mess):
    """Messages sent to the bot will arrive here.
    Command handling + routing is done in this function."""

    typ = mess.getType()
    jid = mess.getFrom()
    props = mess.getProperties()
    text = mess.getBody()
    username = self.__get_sender_username(mess)

    if text is None:
      return

    if typ not in ("groupchat","chat"):
      self.log.debug("unhandled message type: %s" % typ)
      return

    if typ=='groupchat':

      # Ignore messages from before we joined
      if xmpp.NS_DELAY in props:
        return

      # Ignore messages from myself
      room = jid.getStripped()
      if ((self.jid==jid) or
          (room in self.__get_current_mucs()
              and jid.getResource()==self.mucs[room]['nick'])):
        return

    # Ignore messages from users not seen by this bot
    real = [j.getStripped() for j in self.real_jids.values()]
    if (jid not in self.seen) and (jid.getStripped() not in real):
      self.log.info('Ignoring message from unseen guest: %s' % jid)
      self.log.debug("I've seen: %s" %
        ["%s" % x for x in self.seen.keys()+real])
      return

    if len(text)>40:
      txt = text[:40]+'...'
    else:
      txt = text
    self.log.debug('Got %s from %s: "%s"' % (typ,jid,txt))

    typ = self.TYPES[typ]
    frm = JID(jid,typ)
    if jid in self.real_jids:
      real = self.real_jids[jid]
      frm.set_real(JID(real,Message.PRIVATE))
    
    self.bot._cb_message(Message(typ,frm,text))

  def callback_presence(self,conn,pres):
    """run upon receiving a presence stanza to keep track of subscriptions"""

    (jid,typ,show,status) = (pres.getFrom(),pres.getType(),
                              pres.getShow(),pres.getStatus())
    frm = pres.getFrom()

    # keep track of "real" JIDs in a MUC
    # when joining a MUC other member presence might come before confirmation
    real = None
    if ((jid.getStripped() in self.__get_current_mucs()) or
        (jid.getStripped() in [x[0] for x in self.__muc_pending])):
      x_tags = pres.getTags('x')
      for x_tag in x_tags:
        item_tags = x_tag.getTags('item')
        for item_tag in item_tags:
          real = item_tag.getAttr('jid')
          if real is None:
            continue
          self.real_jids[jid] = xmpp.protocol.JID(real)
          self.log.debug('JID: '+str(jid)+' = realJID: '+real)
          break
        if real is not None:
          break

    # update internal status
    if self.jid.bareMatch(jid):
      if typ!=self.OFFLINE:
        self.__status = status
        self.__show = show
      else:
        self.__status = ""
        self.__show = self.OFFLINE
      return

    if typ is None:
      # Keep track of status message and type changes
      old_show, old_status = self.seen.get(jid, (self.OFFLINE, None))
      if old_show != show:
        self.__status_type_changed(jid, show)

      if old_status != status:
        self.__status_message_changed(jid, status)

      self.seen[jid] = (show, status)
    elif typ == self.OFFLINE and jid in self.seen:
      # Notify of user offline status change
      del self.seen[jid]
      self.__status_type_changed(jid, self.OFFLINE)

    try:
      subscription = self.roster.getSubscription(unicode(jid.__str__()))
    except KeyError, e:
      # User not on our roster
      subscription = None
    except AttributeError, e:
      # Recieved presence update before roster built
      return

    self.log.debug('Got presence: %s (type: %s, show: %s, status: %s, '\
      'subscription: %s)' % (jid, typ, show, status, subscription))

    if typ == 'error':
      self.log.error(pres.getError())
      return

    # Catch kicked from the room
    room = jid.getStripped()
    if room in self.__get_current_mucs() and jid.getResource()==self.mucs[room]['nick']:
      code = pres.getStatusCode()
      if code:
        code = int(code)
        if code in self.MUC_CODES:
          self.mucs[room]['status'] = code
          self.last_join = time.time()
          (text,func) = self.MUC_CODES[code]
          func('Forced from room "%s" (%s)' % (room,text))
          self.log.debug('Rejoining room "%s" in %i sec' % (room,self.opt('recon_wait')))
          return

    # If subscription is private,
    # disregard anything not from the private domain
    if typ in ('subscribe','subscribed','unsubscribe','unsubscribed'):
      if self.opt('xmpp.priv_domain')==True:
        # Use the bot's domain
        domain = self.jid.getDomain()
      else:
        # Use the specified domain
        domain = self.opt('xmpp.priv_domain')

      # Check if the sender is in the private domain
      user_domain = jid.getDomain()
      if self.opt('xmpp.priv_domain') and domain!=user_domain:
        self.log.info('Ignoring subscribe request: %s does not '\
        'match private domain (%s)' % (user_domain, domain))
        return

      # Incoming subscription request
      if typ=='subscribe':
        # Authorize all subscription requests (we checked for domain above)
        self.roster.Authorize(jid)
        self.log.debug('authorized!')
        self.__send_status()
      if typ=='unsubscribe':
        # Unsubscribe both directions
        self.roster.Unauthorize(jid)
        self.log.debug('unauthorized!')

      # Do nothing for 'subscribed' or 'unsubscribed'
      return

    # type is unavailable for logged out, otherwise status is in 'show'
    status_msg = status
    if typ=='unavailable':
      status = Message.OFFLINE
    else:
      status = self.STATUS.get(show,Message.UNKNOWN)

    # presence stanzas don't necessarily specify type, so just check if the
    # sending JID contains any of our rooms
    typ = Message.STATUS
    jid_typ = Message.PRIVATE
    real = None
    if jid.getStripped() in self.__get_current_mucs():
      jid_typ = Message.GROUP
      if jid in self.real_jids:
        real = JID(self.real_jids[jid],Message.PRIVATE)
    frm = JID(jid,jid_typ)
    if real:
      frm.set_real(real)

    # call SibylBot's message callback
    self.bot._cb_message(Message(typ,frm,None,status,status_msg))

################################################################################
# Helper functions                                                             #
################################################################################

  def __status_type_changed(self, jid, new_status_type):
    """Callback for tracking status types (dnd, away, offline, ...)"""
    self.log.debug('user %s changed status to %s' % (jid, new_status_type))

  def __status_message_changed(self, jid, new_status_message):
    """Callback for tracking status messages (the free-form status text)"""
    self.log.debug('user %s updated text to %s' %
      (jid, new_status_message))

  def __set_status(self, value):
    """Set status message.
    If value remains constant, no presence stanza will be send"""
    if self.__status != value:
      self.__status = value
      self._send_status()

  def __get_status(self):
    """Get current status message"""
    return self.__status

  status_message = property(fget=__get_status, fset=__set_status)

  def __set_show(self, value):
    """Set show (status type like AWAY, DND etc.).
    If value remains constant, no presence stanza will be send"""
    if self.__show != value:
      self.__show = value
      self._send_status()

  def __get_show(self):
    """Get current show (status type like AWAY, DND etc.)."""
    return self.__show

  status_type = property(fget=__get_show, fset=__set_show)

  def __build_message(self,text):
    """Builds an xhtml message without attributes.
    If input is not valid xhtml-im fallback to normal."""
    message = None  # init message variable
    # Try to determine if text has xhtml-tags - TODO needs improvement
    text_plain = re.sub(r'<[^>]+>', '', text)
    if text_plain != text:
      # Create body w stripped tags for reciptiens w/o xhtml-abilities
      # FIXME unescape &quot; etc.
      message = xmpp.protocol.Message(body=text_plain)
      # Start creating a xhtml body
      html = xmpp.Node('html', \
        {'xmlns': 'http://jabber.org/protocol/xhtml-im'})
      try:
        html.addChild(node=xmpp.simplexml.XML2Node( \
          "<body xmlns='http://www.w3.org/1999/xhtml'>" + \
          text.encode('utf-8') + "</body>"))
        message.addChild(node=html)
      except Exception, e:
        # Didn't work, incorrect markup or something.
        self.log.debug('An error while building a xhtml message. '\
        'Fallback to normal messagebody')
        # Fallback - don't sanitize invalid input. User is responsible!
        message = None
    if message is None:
      # Normal body
      message = xmpp.protocol.Message(body=text)
    return message

  def __send_status(self):
    """Send status to everyone"""
    try:
      self.conn.send(xmpp.dispatcher.Presence(show=self.__show,
        status=self.__status))
    except IOError:
      raise ConnectFailure

  def __idle_proc(self):
    """ping, join pending mucs, and try to rejoin mucs we were forced from"""

    self.__idle_ping()
    self.__idle_join_muc()
    self.__idle_rejoin_muc()

  def __idle_ping(self):
    """send pings to make sure the server is still there"""

    # build a ping stanza and send it if it's been long enough since last ping
    if (self.opt('xmpp.ping_freq')
        and time.time()-self.last_ping>self.opt('xmpp.ping_freq')):
      self.last_ping = time.time()
      payload = [xmpp.Node('ping',attrs={'xmlns':'urn:xmpp:ping'})]
      ping = xmpp.Protocol('iq',typ='get',payload=payload)

      # raise PingTimeout if pinging fails
      try:
        res = self.conn.SendAndWaitForResponse(ping,self.opt('xmpp.ping_timeout'))
      except IOError:
        raise PingTimeout
      if res is None:
        raise PingTimeout

################################################################################
# XEP-0045 Multi User Chat (MUC)                                               #
################################################################################
# Joining a MUC (2 asynchronous execution paths)                               #
#                                                                              #
# (1) User calls self.join_room() which adds MUC info to self.__muc_pending    #
#     with status MUC_PENDING
#                                                                              #
# (1) Approx once per second self.process() is called                          #
# (2) Which calls self.__idle_proc()                                           #
# (3) Which calls self.__idle_join_muc()                                       #
# (4) Which tries to join every MUC in self.__muc_pending                      #
# (5) By calling self.__muc_join() which sends a stanza and checks for success #
# (6) On success self.mucs is updated to have MUC_OK                           #
#     On failure self.mucs is updated to have MUC_PARTED and we won't rejoin   #
################################################################################
# Getting forced from a MUC                                                    #
#                                                                              #
# (1) self.callback_presence() receives a presence that forces us from the MUC #
# (2) Which updates the status in self.mucs to any of those in self.MUC_CODES  #
# (3) This can't result in MUC_PARTED or MUC_OK so we will try to rejoin       #
################################################################################
# Rejoining a MUC                                                              #
#                                                                              #
# (1) Approx once per second self.process() is called                          #
# (2) Which calls self.__idle_proc()                                           #
# (3) Which calls self.__idle_rejoin_muc()                                     #
# (4) Which tries to rejoin every MUC except MUC_PARTED, MUC_PENDING, MUC_OK   #
# (5) By calling self.__muc_join() which sends a stanza and checks for success #
# (6) On success self.mucs is updated to have MUC_OK                           #
#     On failure self.mucs has the error code or MUC_ERR and we will rejoin    #
################################################################################
# Parting a MUC                                                                #
#                                                                              #
# (1) User calls self.part_room()                                              #
# (2) Which sends a stanza and updates self.mucs to have MUC_PARTED            #
# (3) This can be used to either leave a room or cancel reconnection           #
################################################################################

  def __idle_join_muc(self):
    """join pending MUCs"""

    if len(self.__muc_pending):
      try:
        # try to join the first MUC in the queue
        (room,user,pword) = self.__muc_pending[0]
        self.__muc_join(room,user,pword)
        self.__muc_join_success(room)

      # we only rejoin if we were able to rejoin successfully in the past
      except MUCJoinFailure as e:
        self.__muc_join_failure(room,e.message)
        if self.mucs[room]['status'] > self.MUC_OK:
          self.log.debug('Rejoining room "%s" in %i sec' % (room,self.opt('recon_wait')))
        else:
          self.mucs[room]['status'] = self.MUC_PARTED

      # we need this to happen after successful joining to catch presences
      finally:
        del self.__muc_pending[0]

  def __muc_join(self,room,nick,pword):
    """send XMPP stanzas to join a muc and raise MUCJoinFailure if error"""

    # build the room join stanza
    NS_MUC = 'http://jabber.org/protocol/muc'
    nick = self.mucs[room]['nick']
    room_jid = room+'/'+nick

    # request no history and add password if we need one
    pres = xmpp.Presence(to=room_jid)
    pres.setTag('x',namespace=NS_MUC).setTagData('history','',attrs={'maxchars':'0'})
    if pword is not None:
      pres.setTag('x',namespace=NS_MUC).setTagData('password',pword)

    # try to join and wait for response
    self.log.debug('Attempting to join room "%s"' % room)
    self.last_muc = room
    try:
      result = self.conn.SendAndWaitForResponse(pres)
    except IOError:
      raise ConnectFailure

    # result is None for timeout
    if not result:
      self.log.error('Error joining room "%s" (timeout)' % room)
      raise MUCJoinFailure('timeout')

    # check for error
    error = result.getError()
    if error:
      self.log.error('Error joining room "%s" (%s)' % (room,self.MUC_JOIN_ERROR[error]))
      raise MUCJoinFailure(error)

    # we joined successfully
    self.mucs[room]['status'] = self.MUC_OK
    self.log.info('Success joining room "%s"' % room)

  def __muc_join_success(self,room):
    """execute callbacks on successfull MUC join"""

    self.bot._cb_join_room_success(Room(room))

  def __muc_join_failure(self,room,error):
    """execute callbacks on successfull MUC join"""

    self.bot._cb_join_room_failure(Room(room),self.MUC_JOIN_ERROR[error])

  def __idle_rejoin_muc(self):
    """attempt to rejoin the MUC if needed"""

    # don't try too often
    t = time.time()
    if t-self.opt('recon_wait')<self.last_join:
      return

    # we'll keep trying until the user tells us to stop via part_room()
    for room in self.mucs:
      if self.mucs[room]['status']>self.MUC_OK:
        self.__lastjoin = time.time()
        muc = self.mucs[room]
        try:
          self.__muc_join(room,muc['nick'],muc['pass'])
        except MUCJoinFailure:
          pass

  def __get_current_mucs(self):
    """return all mucs that we are currently in"""

    return [room for room in self.mucs if self.mucs[room]['status']==self.MUC_OK]

  def __get_active_mucs(self):
    """return all mucs we are in or are trying to reconnect"""

    return [room for room in self.mucs if self.mucs[room]['status']>=self.MUC_OK]

  def __get_inactive_mucs(self):
    """return all mucs that we are currently not in"""

    return [room for room in self.mucs if self.mucs[room]['status']!=self.MUC_OK]

  def __get_sender_username(self, mess):
    """Extract the sender's user name from a message"""
    type = mess.getType()
    jid = mess.getFrom()
    if type == "groupchat":
      username = jid.getResource()
    elif type == "chat":
      username = jid.getNode()
    else:
      username = ""
    return username
