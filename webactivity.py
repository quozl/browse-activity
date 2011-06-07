# Copyright (C) 2006, Red Hat, Inc.
# Copyright (C) 2009 Martin Langhoff, Simon Schampijer, Daniel Drake,
#                    Tomeu Vizoso
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
from gettext import gettext as _
from gettext import ngettext
import os
import subprocess

import gobject
gobject.threads_init()

import gtk
import base64
import time
import shutil
import sqlite3
import cjson
import gconf
import locale
import cairo
from hashlib import sha1

# HACK: Needed by http://dev.sugarlabs.org/ticket/456
import gnome
gnome.init('Hulahop', '1.0')

from sugar.activity import activity
from sugar.graphics import style
import telepathy
import telepathy.client
from sugar.presence import presenceservice
from sugar.graphics.tray import HTray
from sugar import profile
from sugar.graphics.alert import Alert
from sugar.graphics.icon import Icon
from sugar import mime

# Attempt to import the new toolbar classes.  If the import fails,
# fall back to the old toolbar style.
try:
    from sugar.graphics.toolbarbox import ToolbarButton
    NEW_TOOLBARS = True
except ImportError:
    NEW_TOOLBARS = False

PROFILE_VERSION = 2

_profile_version = 0
_profile_path = os.path.join(activity.get_activity_root(), 'data/gecko')
_version_file = os.path.join(_profile_path, 'version')

if not NEW_TOOLBARS:
    _TOOLBAR_EDIT = 1
    _TOOLBAR_BROWSE = 2

if os.path.exists(_version_file):
    f = open(_version_file)
    _profile_version = int(f.read())
    f.close()

if _profile_version < PROFILE_VERSION:
    if not os.path.exists(_profile_path):
        os.mkdir(_profile_path)

    shutil.copy('cert8.db', _profile_path)
    os.chmod(os.path.join(_profile_path, 'cert8.db'), 0660)

    f = open(_version_file, 'w')
    f.write(str(PROFILE_VERSION))
    f.close()


def _seed_xs_cookie():
    ''' Create a HTTP Cookie to authenticate with the Schoolserver
    '''
    client = gconf.client_get_default()
    backup_url = client.get_string('/desktop/sugar/backup_url')
    if not backup_url:
        _logger.debug('seed_xs_cookie: Not registered with Schoolserver')
        return

    jabber_server = client.get_string(
        '/desktop/sugar/collaboration/jabber_server')

    pubkey = profile.get_profile().pubkey
    cookie_data = {'color': profile.get_color().to_string(),
                   'pkey_hash': sha1(pubkey).hexdigest()}

    db_path = os.path.join(_profile_path, 'cookies.sqlite')
    try:
        cookies_db = sqlite3.connect(db_path)
        c = cookies_db.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS
                     moz_cookies
                     (id INTEGER PRIMARY KEY,
                      name TEXT,
                      value TEXT,
                      host TEXT,
                      path TEXT,
                      expiry INTEGER,
                      lastAccessed INTEGER,
                      isSecure INTEGER,
                      isHttpOnly INTEGER)''')

        c.execute('''SELECT id
                     FROM moz_cookies
                     WHERE name=? AND host=? AND path=?''',
                  ('xoid', jabber_server, '/'))

        if c.fetchone():
            _logger.debug('seed_xs_cookie: Cookie exists already')
            return

        expire = int(time.time()) + 10 * 365 * 24 * 60 * 60
        c.execute('''INSERT INTO moz_cookies (name, value, host,
                                              path, expiry, lastAccessed,
                                              isSecure, isHttpOnly)
                     VALUES(?,?,?,?,?,?,?,?)''',
                  ('xoid', cjson.encode(cookie_data), jabber_server,
                   '/', expire, 0, 0, 0))
        cookies_db.commit()
        cookies_db.close()
    except sqlite3.Error:
        _logger.exception('seed_xs_cookie: could not write cookie')
    else:
        _logger.debug('seed_xs_cookie: Updated cookie successfully')


import hulahop
hulahop.set_app_version(os.environ['SUGAR_BUNDLE_VERSION'])
hulahop.startup(_profile_path)

from xpcom import components


def _set_char_preference(name, value):
    cls = components.classes["@mozilla.org/preferences-service;1"]
    prefService = cls.getService(components.interfaces.nsIPrefService)
    branch = prefService.getBranch('')
    branch.setCharPref(name, value)


def _set_accept_languages():
    """Set intl.accept_languages preference based on the locale"""

    lang = locale.getdefaultlocale()[0]
    if not lang:
        _logger.debug("Set_Accept_language: unrecognised LANG format")
        return
    lang = lang.split('_')

    # e.g. es-uy, es
    pref = lang[0] + "-" + lang[1].lower() + ", " + lang[0]
    _set_char_preference('intl.accept_languages', pref)
    logging.debug('LANG set')

from browser import TabbedView
from browser import Browser
from webtoolbar import PrimaryToolbar
from edittoolbar import EditToolbar
from viewtoolbar import ViewToolbar
import downloadmanager
import globalhistory
import filepicker

_LIBRARY_PATH = '/usr/share/library-common/index.html'

from model import Model
from sugar.presence.tubeconn import TubeConnection
from messenger import Messenger
from linkbutton import LinkButton

SERVICE = "org.laptop.WebActivity"
IFACE = SERVICE
PATH = "/org/laptop/WebActivity"

_logger = logging.getLogger('web-activity')


class WebActivity(activity.Activity):
    def __init__(self, handle):
        activity.Activity.__init__(self, handle)

        _logger.debug('Starting the web activity')

        self._force_close = False
        self._tabbed_view = TabbedView()

        _set_accept_languages()
        _seed_xs_cookie()

        # don't pick up the sugar theme - use the native mozilla one instead
        cls = components.classes['@mozilla.org/preferences-service;1']
        pref_service = cls.getService(components.interfaces.nsIPrefService)
        branch = pref_service.getBranch("mozilla.widget.")
        branch.setBoolPref("disable-native-theme", True)

        # HACK
        # Currently, the multiple tabs feature crashes the Browse activity
        # on cairo versions 1.8.10 or later. The exact cause for this
        # isn't exactly known. Thus, disable the multiple tabs feature
        # if we come across cairo versions >= 1.08.10
        # More information can be found here:
        # http://lists.sugarlabs.org/archive/sugar-devel/2010-July/025187.html
        self._disable_multiple_tabs = cairo.cairo_version() >= 10810
        if self._disable_multiple_tabs:
            logging.warning('Not enabling the multiple tabs feature due'
                ' to a bug in cairo/mozilla')

        self._tray = HTray()
        self.set_tray(self._tray, gtk.POS_BOTTOM)
        self._tray.show()

        self._primary_toolbar = PrimaryToolbar(self._tabbed_view, self,
                    self._disable_multiple_tabs)
        self._edit_toolbar = EditToolbar(self)
        self._view_toolbar = ViewToolbar(self)

        self._primary_toolbar.connect('add-link', self._link_add_button_cb)

        self._primary_toolbar.connect('add-tab', self._new_tab_cb)

        self._primary_toolbar.connect('go-home', self._go_home_button_cb)

        if NEW_TOOLBARS:
            logging.debug('Using new toolbars')

            self._edit_toolbar_button = ToolbarButton(
                    page=self._edit_toolbar,
                    icon_name='toolbar-edit')
            self._primary_toolbar.toolbar.insert(
                    self._edit_toolbar_button, 1)

            view_toolbar_button = ToolbarButton(
                    page=self._view_toolbar,
                    icon_name='toolbar-view')
            self._primary_toolbar.toolbar.insert(
                    view_toolbar_button, 2)

            self._primary_toolbar.show_all()
            self.set_toolbar_box(self._primary_toolbar)
        else:
            _logger.debug('Using old toolbars')

            toolbox = activity.ActivityToolbox(self)

            toolbox.add_toolbar(_('Edit'), self._edit_toolbar)
            self._edit_toolbar.show()

            toolbox.add_toolbar(_('Browse'), self._primary_toolbar)
            self._primary_toolbar.show()

            toolbox.add_toolbar(_('View'), self._view_toolbar)
            self._view_toolbar.show()

            self.set_toolbox(toolbox)
            toolbox.show()

            self.toolbox.set_current_toolbar(_TOOLBAR_BROWSE)

        self.set_canvas(self._tabbed_view)
        self._tabbed_view.show()

        self.model = Model()
        self.model.connect('add_link', self._add_link_model_cb)

        self.connect('key-press-event', self._key_press_cb)

        if handle.uri:
            self._tabbed_view.current_browser.load_uri(handle.uri)
        elif not self._jobject.file_path:
            # TODO: we need this hack until we extend the activity API for
            # opening URIs and default docs.
            self._load_homepage()

        self.messenger = None
        self.connect('shared', self._shared_cb)

        # Get the Presence Service
        self.pservice = presenceservice.get_instance()
        try:
            name, path = self.pservice.get_preferred_connection()
            self.tp_conn_name = name
            self.tp_conn_path = path
            self.conn = telepathy.client.Connection(name, path)
        except TypeError:
            _logger.debug('Offline')
        self.initiating = None

        if self._shared_activity is not None:
            _logger.debug('shared: %s', self._shared_activity.props.joined)

        if self._shared_activity is not None:
            # We are joining the activity
            _logger.debug('Joined activity')
            self.connect('joined', self._joined_cb)
            if self.get_shared():
                # We've already joined
                self._joined_cb()
        else:
            _logger.debug('Created activity')

    def _new_tab_cb(self, gobject):
        self._load_homepage(new_tab=True)

    def _shared_cb(self, activity_):
        _logger.debug('My activity was shared')
        self.initiating = True
        self._setup()

        _logger.debug('This is my activity: making a tube...')
        self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].OfferDBusTube(SERVICE,
                                                                    {})

    def _setup(self):
        if self._shared_activity is None:
            _logger.debug('Failed to share or join activity')
            return

        bus_name, conn_path, channel_paths = \
                self._shared_activity.get_channels()

        # Work out what our room is called and whether we have Tubes already
        room = None
        tubes_chan = None
        text_chan = None
        for channel_path in channel_paths:
            channel = telepathy.client.Channel(bus_name, channel_path)
            htype, handle = channel.GetHandle()
            if htype == telepathy.HANDLE_TYPE_ROOM:
                _logger.debug('Found our room: it has handle#%d "%s"',
                              handle,
                              self.conn.InspectHandles(htype, [handle])[0])
                room = handle
                ctype = channel.GetChannelType()
                if ctype == telepathy.CHANNEL_TYPE_TUBES:
                    _logger.debug('Found our Tubes channel at %s',
                                  channel_path)
                    tubes_chan = channel
                elif ctype == telepathy.CHANNEL_TYPE_TEXT:
                    _logger.debug('Found our Text channel at %s',
                                  channel_path)
                    text_chan = channel

        if room is None:
            _logger.debug("Presence service didn't create a room")
            return
        if text_chan is None:
            _logger.debug("Presence service didn't create a text channel")
            return

        # Make sure we have a Tubes channel - PS doesn't yet provide one
        if tubes_chan is None:
            _logger.debug("Didn't find our Tubes channel, requesting one...")
            tubes_chan = self.conn.request_channel(
                telepathy.CHANNEL_TYPE_TUBES, telepathy.HANDLE_TYPE_ROOM,
                room, True)

        self.tubes_chan = tubes_chan
        self.text_chan = text_chan

        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal( \
                'NewTube', self._new_tube_cb)

    def _list_tubes_reply_cb(self, tubes):
        for tube_info in tubes:
            self._new_tube_cb(*tube_info)

    def _list_tubes_error_cb(self, e):
        _logger.debug('ListTubes() failed: %s', e)

    def _joined_cb(self, activity_):
        if not self._shared_activity:
            return

        _logger.debug('Joined an existing shared activity')

        self.initiating = False
        self._setup()

        _logger.debug('This is not my activity: waiting for a tube...')
        self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self._list_tubes_reply_cb,
            error_handler=self._list_tubes_error_cb)

    def _new_tube_cb(self, identifier, initiator, type, service, params,
                     state):
        _logger.debug('New tube: ID=%d initator=%d type=%d service=%s '
                      'params=%r state=%d', identifier, initiator, type,
                      service, params, state)

        if (type == telepathy.TUBE_TYPE_DBUS and
            service == SERVICE):
            if state == telepathy.TUBE_STATE_LOCAL_PENDING:
                self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].AcceptDBusTube(
                        identifier)

            self.tube_conn = TubeConnection(self.conn,
                self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES],
                identifier, group_iface=self.text_chan[
                    telepathy.CHANNEL_INTERFACE_GROUP])

            _logger.debug('Tube created')
            self.messenger = Messenger(self.tube_conn, self.initiating,
                                       self.model)

    def _load_homepage(self, new_tab=False):
        # If new_tab is True, open the homepage in a new tab.
        if new_tab:
            browser = Browser()
            self._tabbed_view._append_tab(browser)
        else:
            browser = self._tabbed_view.current_browser

        if os.path.isfile(_LIBRARY_PATH):
            browser.load_uri('file://' + _LIBRARY_PATH)
        else:
            default_page = os.path.join(activity.get_bundle_path(),
                                        "data/index.html")
            browser.load_uri(default_page)

    def _get_data_from_file_path(self, file_path):
        fd = open(file_path, 'r')
        try:
            data = fd.read()
        finally:
            fd.close()
        return data

    def read_file(self, file_path):
        if self.metadata['mime_type'] == 'text/plain':
            data = self._get_data_from_file_path(file_path)
            self.model.deserialize(data)

            for link in self.model.data['shared_links']:
                _logger.debug('read: url=%s title=%s d=%s' % (link['url'],
                                                              link['title'],
                                                              link['color']))
                self._add_link_totray(link['url'],
                                      base64.b64decode(link['thumb']),
                                      link['color'], link['title'],
                                      link['owner'], -1, link['hash'])
            logging.debug('########## reading %s', data)
            self._tabbed_view.set_session(self.model.data['history'])
            self._tabbed_view.set_current_page(self.model.data['current_tab'])
        elif self.metadata['mime_type'] == 'text/uri-list':
            data = self._get_data_from_file_path(file_path)
            uris = mime.split_uri_list(data)
            if len(uris) == 1:
                self._tabbed_view.props.current_browser.load_uri(uris[0])
            else:
                _logger.error('Open uri-list: Does not support'
                              'list of multiple uris by now.')
        else:
            self._tabbed_view.props.current_browser.load_uri(file_path)
        self._load_urls()

    def _load_urls(self):
        if self.model.data['currents'] != None:
            first = True
            for current_tab in self.model.data['currents']:
                if first:
                    browser = self._tabbed_view.current_browser
                    first = False
                else:
                    browser = Browser()
                    self._tabbed_view._append_tab(browser)
                browser.load_uri(current_tab['url'])

    def write_file(self, file_path):
        if not self.metadata['mime_type']:
            self.metadata['mime_type'] = 'text/plain'

        if self.metadata['mime_type'] == 'text/plain':

            browser = self._tabbed_view.current_browser

            if not self._jobject.metadata['title_set_by_user'] == '1':
                if browser.props.title:
                    self.metadata['title'] = browser.props.title

            self.model.data['history'] = self._tabbed_view.get_session()
            current_tab = self._tabbed_view.get_current_page()
            self.model.data['current_tab'] = current_tab

            self.model.data['currents'] = []
            for n in range(0, self._tabbed_view.get_n_pages()):
                n_browser = self._tabbed_view.get_nth_page(n)
                if n_browser != None:
                    nsiuri = browser.progress.location
                    ui_uri = browser.get_url_from_nsiuri(nsiuri)
                    info = {'title': browser.props.title, 'url': ui_uri}
                    self.model.data['currents'].append(info)

            f = open(file_path, 'w')
            try:
                logging.debug('########## writing %s', self.model.serialize())
                f.write(self.model.serialize())
            finally:
                f.close()

    def _link_add_button_cb(self, button):
        self._add_link()

    def _go_home_button_cb(self, button):
        self._load_homepage()

    def _key_press_cb(self, widget, event):
        key_name = gtk.gdk.keyval_name(event.keyval)
        browser = self._tabbed_view.props.current_browser

        if event.state & gtk.gdk.CONTROL_MASK:

            if key_name == 'd':
                self._add_link()
            elif key_name == 'f':
                _logger.debug('keyboard: Find')
                if NEW_TOOLBARS:
                    self._edit_toolbar_button.set_expanded(True)
                else:
                    self.toolbox.set_current_toolbar(_TOOLBAR_EDIT)
                self._edit_toolbar.search_entry.grab_focus()
            elif key_name == 'l':
                _logger.debug('keyboard: Focus url entry')
                if not NEW_TOOLBARS:
                    self.toolbox.set_current_toolbar(_TOOLBAR_BROWSE)
                self._primary_toolbar.entry.grab_focus()
            elif key_name == 'minus':
                _logger.debug('keyboard: Zoom out')
                browser.zoom_out()
            elif key_name in ['plus', 'equal']:
                _logger.debug('keyboard: Zoom in')
                browser.zoom_in()
            elif key_name == 'Left':
                browser.web_navigation.goBack()
            elif key_name == 'Right':
                browser.web_navigation.goForward()
            elif key_name == 'r':
                flags = components.interfaces.nsIWebNavigation.LOAD_FLAGS_NONE
                browser.web_navigation.reload(flags)
            elif gtk.gdk.keyval_name(event.keyval) == "t":
                if not self._disable_multiple_tabs:
                    self._load_homepage(new_tab=True)
            else:
                return False

            return True

        return False

    def _add_link(self):
        ''' take screenshot and add link info to the model '''

        browser = self._tabbed_view.props.current_browser
        ui_uri = browser.get_url_from_nsiuri(browser.progress.location)

        for link in self.model.data['shared_links']:
            if link['hash'] == sha1(ui_uri).hexdigest():
                _logger.debug('_add_link: link exist already a=%s b=%s',
                              link['hash'], sha1(ui_uri).hexdigest())
                return
        buf = self._get_screenshot()
        timestamp = time.time()
        self.model.add_link(ui_uri, browser.props.title, buf,
                            profile.get_nick_name(),
                            profile.get_color().to_string(), timestamp)

        if self.messenger is not None:
            self.messenger._add_link(ui_uri, browser.props.title,
                                     profile.get_color().to_string(),
                                     profile.get_nick_name(),
                                     base64.b64encode(buf), timestamp)

    def _add_link_model_cb(self, model, index):
        ''' receive index of new link from the model '''
        link = self.model.data['shared_links'][index]
        self._add_link_totray(link['url'], base64.b64decode(link['thumb']),
                              link['color'], link['title'],
                              link['owner'], index, link['hash'])

    def _add_link_totray(self, url, buf, color, title, owner, index, hash):
        ''' add a link to the tray '''
        item = LinkButton(url, buf, color, title, owner, index, hash)
        item.connect('clicked', self._link_clicked_cb, url)
        item.connect('remove_link', self._link_removed_cb)
        # use index to add to the tray
        self._tray.add_item(item, index)
        item.show()
        if self._tray.props.visible is False:
            self._tray.show()
        self._view_toolbar.traybutton.props.sensitive = True

    def _link_removed_cb(self, button, hash):
        ''' remove a link from tray and delete it in the model '''
        self.model.remove_link(hash)
        self._tray.remove_item(button)
        if len(self._tray.get_children()) == 0:
            self._view_toolbar.traybutton.props.sensitive = False

    def _link_clicked_cb(self, button, url):
        ''' an item of the link tray has been clicked '''
        self._tabbed_view.props.current_browser.load_uri(url)

    def _pixbuf_save_cb(self, buf, data):
        data[0] += buf
        return True

    def get_buffer(self, pixbuf):
        data = [""]
        pixbuf.save_to_callback(self._pixbuf_save_cb, "png", {}, data)
        return str(data[0])

    def _get_screenshot(self):
        window = self._tabbed_view.props.current_browser.window
        width, height = window.get_size()

        screenshot = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, has_alpha=False,
                                    bits_per_sample=8, width=width,
                                    height=height)
        screenshot.get_from_drawable(window, window.get_colormap(), 0, 0, 0, 0,
                                     width, height)

        screenshot = screenshot.scale_simple(style.zoom(100),
                                                 style.zoom(80),
                                                 gtk.gdk.INTERP_BILINEAR)

        buf = self.get_buffer(screenshot)
        return buf

    def can_close(self):
        if self._force_close:
            return True
        elif downloadmanager.can_quit():
            return True
        else:
            alert = Alert()
            alert.props.title = ngettext('Download in progress',
                                         'Downloads in progress',
                                         downloadmanager.num_downloads())
            message = ngettext('Stopping now will erase your download',
                               'Stopping now will erase your downloads',
                               downloadmanager.num_downloads())
            alert.props.msg = message
            cancel_icon = Icon(icon_name='dialog-cancel')
            cancel_label = ngettext('Continue download', 'Continue downloads',
                                    downloadmanager.num_downloads())
            alert.add_button(gtk.RESPONSE_CANCEL, cancel_label, cancel_icon)
            stop_icon = Icon(icon_name='dialog-ok')
            alert.add_button(gtk.RESPONSE_OK, _('Stop'), stop_icon)
            stop_icon.show()
            self.add_alert(alert)
            alert.connect('response', self.__inprogress_response_cb)
            alert.show()
            self.present()
            return False

    def __inprogress_response_cb(self, alert, response_id):
        self.remove_alert(alert)
        if response_id is gtk.RESPONSE_CANCEL:
            logging.debug('Keep on')
        elif response_id == gtk.RESPONSE_OK:
            logging.debug('Stop downloads and quit')
            self._force_close = True
            downloadmanager.remove_all_downloads()
            self.close()

    def get_document_path(self, async_cb, async_err_cb):
        browser = self._tabbed_view.props.current_browser
        browser.get_source(async_cb, async_err_cb)

    def get_canvas(self):
        return self._tabbed_view
