# -*- coding: utf-8 -*-

# Copyright (c) 2012 Jiri "NoxArt" Petruzelka
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

# @author Jiri "NoxArt" Petruzelka | petruzelka@noxart.cz | @NoxArt
# @copyright (c) 2012 Jiri "NoxArt" Petruzelka
# @link https://github.com/NoxArt/SublimeText2-FTPSync

# Doc comment syntax inspired by http://stackoverflow.com/a/487203/387503


# ==== Libraries ===========================================================================

# Sublime API see http://www.sublimetext.com/docs/2/api_reference.html
import sublime
import sublime_plugin

# Python's built-in libraries
import shutil
import os
import hashlib
import json
import threading
import re
import copy
import traceback
import sys

# FTPSync libraries
from ftpsyncwrapper import CreateConnection, TargetAlreadyExists
from ftpsyncprogress import Progress
from ftpsyncfiles import getFolders, findFile, getFiles, formatTimestamp, gatherMetafiles, getChangedFiles


# ==== Initialization and optimization =====================================================

# global config
settings = sublime.load_settings('ftpsync.sublime-settings')


# print debug messages to console?
isDebug = settings.get('debug')
# print overly informative messages?
isDebugVerbose = settings.get('debug_verbose')
# default config for a project
projectDefaults = settings.get('project_defaults').items()
nested = []
index = 0
for item in projectDefaults:
    if type(item[1]) is dict:
        nested.append(index)
    index += 1

# global ignore pattern
ignore = settings.get('ignore')
# time format settings
time_format = settings.get('time_format')
# delay before check of right opened file is performed, cancelled if closed in the meantime
download_on_open_delay = settings.get('download_on_open_delay')

# loaded project's config will be merged with this global one
coreConfig = {
    'ignore': ignore,
    'connection_timeout': settings.get('connection_timeout'),
    'ascii_extensions': settings.get('ascii_extensions'),
    'binary_extensions': settings.get('binary_extensions')
}.items()

# compiled global ignore pattern
if type(ignore) is str or type(ignore) is unicode:
    re_ignore = re.compile(ignore)
else:
    re_ignore = None


# name of a file to be detected in the project
configName = 'ftpsync.settings'
# name of a file that is a default sheet for new configs for projects
connectionDefaultsFilename = 'ftpsync.default-settings'
# timeout for a Sublime status bar messages [ms]
messageTimeout = 250
# comment removing regexp
removeLineComment = re.compile('//.*', re.I)
# deprecated names
deprecatedNames = {
    "check_time": "overwrite_newer_prevention"
}


# connection cache pool - all connections
connections = {}
# connections currently marked as {in use}
usingConnections = []
# individual folder config cache, file => config path
configs = {}
# scheduled delayed uploads, file_path => action id
scheduledUploads = {}


# ==== Generic =============================================================================

# Returns whether the variable is some form os string
def isString(var):
    var_type = type(var)
    return var_type is str or var_type is unicode

# Dumps the exception to console
def handleException(exception):
    print "FTPSync > Exception in user code:"
    print '-' * 60
    traceback.print_exc(file=sys.stdout)
    print '-' * 60


# Safer print of exception message
def stringifyException(exception):
    return unicode(exception)


# ==== Messaging ===========================================================================

# Shows a message into Sublime's status bar
#
# @type  text: string
# @param text: message to status bar
def statusMessage(text):
    sublime.status_message(text)


# Schedules a single message to be logged/shown
#
# @type  text: string
# @param text: message to status bar
#
# @global messageTimeout
def dumpMessage(text):
    sublime.set_timeout(lambda: statusMessage(text), messageTimeout)


# Prints a special message to console and optionally to status bar
#
# @type  text: string
# @param text: message to status bar
# @type  name: string|None
# @param name: comma-separated list of connections or other auxiliary info
# @type  onlyVerbose: boolean
# @param onlyVerbose: print only if config has debug_verbose enabled
# @type  status: boolean
# @param status: show in status bar as well = true
#
# @global isDebug
# @global isDebugVerbose
def printMessage(text, name=None, onlyVerbose=False, status=False):
    message = "FTPSync"

    if name is not None:
        message += " [" + unicode(name) + "]"

    message += " > "
    message += unicode(text)

    if isDebug and (onlyVerbose is False or isDebugVerbose is True):
        print message

    if status:
        dumpMessage(message)


# ==== Config =============================================================================

# Invalidates all config cache entries belonging to a certain directory
# as long as they're empty or less nested in the filesystem
#
# @type  config_dir_name: string
# @param config_dir_name: path to a folder of a config to be invalidated
#
# @global configs
def invalidateConfigCache(config_dir_name):
    for file_path in configs:
        if file_path.startswith(config_dir_name) and (configs[file_path] is None or config_dir_name.startswith(configs[file_path])):
            configs.remove(configs[file_path])


# Finds a config file in given folders
#
# @type  folders: list<string>
# @param folders: list of paths to folders to filter
#
# @return list<string> of file paths
#
# @global configName
def findConfigFile(folders):
    return findFile(folders, configName)


# Returns configuration file for a given file
#
# @type  file_path: string
# @param file_path: file_path to the file for which we try to find a config
#
# @return file path to the config file or None
#
# @global configs
def getConfigFile(file_path):
    # try cached
    try:
        if configs[file_path]:
            printMessage("Loading config: cache hit (key: " + file_path + ")")

        return configs[file_path]

    # cache miss
    except KeyError:
        try:
            folders = getFolders(file_path)

            if folders is None or len(folders) == 0:
                return None

            configFolder = findConfigFile(folders)

            if configFolder is None:
                printMessage("Found no config for {" + file_path + "}")
                return None

            config = os.path.join(configFolder, configName)
            configs[file_path] = config
            return config

        except AttributeError:
            return None


# Returns hash of file_path
#
# @type  file_path: string
# @param file_path: file path to the file of which we want the hash
#
# @return hash of filepath
def getFilepathHash(file_path):
    return hashlib.md5(file_path).hexdigest()


# Returns hash of configuration contents
#
# @type config: dict
#
# @return string
#
# @link http://stackoverflow.com/a/8714242/387503
def getObjectHash(o):
    if isinstance(o, set) or isinstance(o, tuple) or isinstance(o, list):
        return tuple([getObjectHash(e) for e in o])
    elif not isinstance(o, dict):
        return hash(o)

    new_o = copy.deepcopy(o)
    for k, v in new_o.items():
        new_o[k] = getObjectHash(v)

    return hash(tuple(frozenset(new_o.items())))


# Updates deprecated config to newer version
#
# @type config: dict
#
# @return dict (config)
#
# @global deprecatedNames
def updateConfig(config):
    for old_name in deprecatedNames:
        new_name = deprecatedNames[old_name]

        if new_name in config:
            config[old_name] = config[new_name]
        elif old_name in config:
            config[new_name] = config[old_name]

    return config


# Verifies contents of a given config object
#
# Checks that it's an object with all needed keys of a proper type
# Does not check semantic validity of the content
#
# Should be used on configs merged with the defaults
#
# @type  config: dict
# @param config: config dict
#
# @return string verification fail reason or a boolean
def verifyConfig(config):
    if type(config) is not dict:
        return "Config is not a {dict} type"

    keys = ["username", "password", "private_key", "private_key_pass", "path", "tls", "upload_on_save", "port", "timeout", "ignore", "check_time", "download_on_open", "upload_delay", "after_save_watch","time_offset"]

    for key in keys:
        if key not in config:
            return "Config is missing a {" + key + "} key"

    if config['username'] is not None and isString(config['username']) is False:
        return "Config entry 'username' must be null or string, " + unicode(type(config['username'])) + " given"

    if config['password'] is not None and isString(config['password']) is False:
        return "Config entry 'password' must be null or string, " + unicode(type(config['password'])) + " given"

    if config['private_key'] is not None and isString(config['private_key']) is False:
        return "Config entry 'private_key' must be null or string, " + unicode(type(config['private_key'])) + " given"

    if config['private_key_pass'] is not None and isString(config['private_key_pass']) is False:
        return "Config entry 'private_key_pass' must be null or string, " + unicode(type(config['private_key_pass'])) + " given"

    if config['ignore'] is not None and isString(config['ignore']) is False:
        return "Config entry 'ignore' must be null or string, " + unicode(type(config['ignore'])) + " given"

    if isString(config['path']) is False:
        return "Config entry 'path' must be a string, " + unicode(type(config['path'])) + " given"

    if type(config['tls']) is not bool:
        return "Config entry 'tls' must be true or false, " + unicode(type(config['tls'])) + " given"

    if type(config['passive']) is not bool:
        return "Config entry 'passive' must be true or false, " + unicode(type(config['passive'])) + " given"

    if type(config['upload_on_save']) is not bool:
        return "Config entry 'upload_on_save' must be true or false, " + unicode(type(config['upload_on_save'])) + " given"

    if type(config['check_time']) is not bool:
        return "Config entry 'check_time' must be true or false, " + unicode(type(config['check_time'])) + " given"

    if type(config['download_on_open']) is not bool:
        return "Config entry 'download_on_open' must be true or false, " + unicode(type(config['download_on_open'])) + " given"

    if type(config['upload_delay']) is not int and type(config['upload_delay']) is not long:
        return "Config entry 'upload_delay' must be integer or long, " + unicode(type(config['upload_delay'])) + " given"

    if config['after_save_watch'] is not None and type(config['after_save_watch']) is not list:
        return "Config entry 'after_save_watch' must be null or list, " + unicode(type(config['after_save_watch'])) + " given"

    if type(config['port']) is not int and type(config['port']) is not long:
        return "Config entry 'port' must be an integer or long, " + unicode(type(config['port'])) + " given"

    if type(config['timeout']) is not int and type(config['timeout']) is not long:
        return "Config entry 'timeout' must be an integer or long, " + unicode(type(config['timeout'])) + " given"

    if type(config['time_offset']) is not int and type(config['time_offset']) is not long:
        return "Config entry 'time_offset' must be an integer or long, " + unicode(type(config['time_offset'])) + " given"

    return True


# Parses JSON-type file with comments stripped out (not part of a proper JSON, see http://json.org/)
#
# @type  file_path: string
#
# @return dict
#
# @global removeLineComment
def parseJson(file_path):
    contents = ""

    try:
        file = open(file_path, 'r')

        for line in file:
            contents += removeLineComment.sub('', line)
    finally:
        file.close()

    return json.loads(contents)


# Parses given config and adds default values to each connection entry
#
# @type  file_path: string
# @param file_path: file path to the file of which we want the hash
#
# @return config dict or None
#
# @global coreConfig
# @global projectDefaults
def loadConfig(file_path):
    if os.path.exists(file_path) is False:
        return None

    # parse config
    try:
        config = parseJson(file_path)
    except Exception, e:
        printMessage("Failed parsing configuration file: {" + file_path + "} (commas problem?) <Exception: " + stringifyException(e) + ">", status=True)
        handleException(e)
        return None

    result = {}

    # merge with defaults and check
    for name in config:
        result[name] = dict(projectDefaults + config[name].items())
        result[name]['file_path'] = file_path

        # merge nested
        for index in nested:
            result[name][projectDefaults[index][0]] = dict(projectDefaults[index][1].items() + result[name][projectDefaults[index][0]].items())
        try:
            if result[name]['debug_extras']['dump_config_load'] is True:
                printMessage(result[name])
        except KeyError:
            pass

        result[name] = updateConfig(result[name])

        verification_result = verifyConfig(result[name])

        if verification_result is not True:
            printMessage("Invalid configuration loaded: <" + unicode(verification_result) + ">", status=True)

    # merge with generics
    final = dict(coreConfig + {"connections": result}.items())

    return final


# ==== Remote =============================================================================

# Returns connection, connects if needed
#
# @type  hash: string
# @param hash: connection cache hash (config filepath hash actually)
# @type  config: object
# @param config: configuration object
#
# @return dict of descendants of AbstractConnection (ftpsyncwrapper.py)
#
# @global connections
def getConnection(hash, config):
    # try cache
    try:
        if connections[hash] and len(connections[hash]) > 0:
            printMessage("Connection cache hit (key: " + hash + ")", None, True)

        if type(connections[hash]) is not list or len(connections[hash]) < len(config['connections']):
            raise KeyError

        # has config changed?
        valid = True
        index = 0
        for name in config['connections']:
            if getObjectHash(connections[hash][index].config) != getObjectHash(config['connections'][name]):
                valid = False

            index += 1

        if valid == False:
            for connection in connections[hash]:
                connection.close(connections, hash)

            raise KeyError

        # is config truly alive
        for connection in connections[hash]:
            if connection.isAlive() is False:
                raise KeyError

        return connections[hash]

    # cache miss
    except KeyError:
        connections[hash] = []

        # for each config
        for name in config['connections']:
            properties = config['connections'][name]

            # 1. initialize
            try:
                connection = CreateConnection(config, name)
            except Exception, e:
                printMessage("Connection initialization failed <Exception: " + stringifyException(e) + ">", name, status=True)
                handleException(e)

                continue

            # 2. connect
            try:
                connection.connect()
            except Exception, e:
                printMessage("Connection failed <Exception: " + stringifyException(e) + ">", name, status=True)
                connection.close(connections, hash)
                handleException(e)

                continue

            printMessage("Connected to: " + properties['host'] + ":" + unicode(properties['port']) + " (timeout: " + unicode(properties['timeout']) + ") (key: " + hash + ")", name)

            # 3. authenticate
            try:
                if connection.authenticate():
                    printMessage("Authentication processed", name)
            except Exception, e:
                printMessage("Authentication failed <Exception: " + stringifyException(e) + ">", name, status=True)
                handleException(e)

                continue

            # 4. login
            if properties['username'] is not None:
                try:
                    connection.login()
                except Exception, e:
                    printMessage("Login failed <Exception: " + stringifyException(e) + ">", name, status=True)
                    handleException(e)

                    continue

                pass_present = " (using password: NO)"
                if len(properties['password']) > 0:
                    pass_present = " (using password: YES)"

                printMessage("Logged in as: " + properties['username'] + pass_present, name)
            else:
                printMessage("Anonymous connection", name)

            # 5. set initial directory, set name, store connection
            try:
                connection.cwd(properties['path'])
            except Exception, e:
                printMessage("Failed to set path (probably connection failed) <Exception: " + stringifyException(e) + ">", name)
                handleException(e)

                continue

            # 6. add to connectins list
            present = False
            for con in connections[hash]:
                if con.name == connection.name:
                    present = True

            if present is False:
                connections[hash].append(connection)

        # schedule connection timeout
        def closeThisConnection():
            if hash not in usingConnections:
                closeConnection(hash)
            else:
                sublime.set_timeout(closeThisConnection, config['connection_timeout'] * 1000)

        sublime.set_timeout(closeThisConnection, config['connection_timeout'] * 1000)

        # return all connections
        return connections[hash]


# Close all connections for a given config file
#
# @type  hash: string
# @param hash: connection cache hash (config filepath hash actually)
#
# @global connections
def closeConnection(hash):
    if isString(hash) is False:
        printMessage("Error closing connection: connection hash must be a string, " + unicode(type(hash)) + " given")
        return

    if hash not in connections:
        return

    try:
        for connection in connections[hash]:
            connection.close(connections, hash)
            printMessage("closed", connection.name)

        if len(connections[hash]) == 0:
            connections.pop(hash)

    except Exception, e:
        printMessage("Error when closing connection (key: " + hash + ") <Exception: " + stringifyException(e) + ">")
        handleException(e)


# Creates a process message with progress bar (to be used in status bar)
#
# @type  stored: list<string>
# @param stored: usually list of connection names
# @type progress: Progress
# @type action: string
# @type action: action that the message reports about ("uploaded", "downloaded"...)
# @type  basename: string
# @param basename: name of a file connected with the action
#
# @return string message
def getProgressMessage(stored, progress, action, basename):
    base = "FTPSync [remotes: " + ",".join(stored) + "] "
    action = "> " + action + " "

    if progress is not None:
        base += " ["

        percent = progress.getPercent()

        for i in range(0, int(percent)):
            base += "="
        for i in range(int(percent), 20):
            base += "--"

        base += " " + unicode(progress.current) + "/" + unicode(progress.getTotal()) + "] "

    return base + action + " {" + basename + "}"


# ==== Executive functions ======================================================================

# Generic synchronization command
class SyncCommand(object):

    def __init__(self, file_path, config_file_path):
        self.closed = False
        self.file_path = file_path
        self.config_file_path = config_file_path

        if isString(config_file_path) is False:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": invalid config_file_path given (type: " + unicode(type(config_file_path)) + ")")
            self.close()
            return

        self.config = loadConfig(config_file_path)
        self.basename = os.path.relpath(file_path, os.path.dirname(config_file_path))

        self.config_hash = getFilepathHash(self.config_file_path)
        self.connections = getConnection(self.config_hash, self.config)

    def _localizePath(self, config, remote_path):
        path = remote_path
        if path.find(config['path']) == 0:
            path = os.path.abspath(os.path.join(os.path.dirname(self.config_file_path), remote_path[len(config['path']):]))

        return path

    def execute(self):
        raise NotImplementedError("Abstract method")

    def close(self):
        self.closed = True

    def _closeConnection(self):
        closeConnection(getFilepathHash(self.config_file_path))

    def whitelistConnections(self, whitelistConnections):
        toBeRemoved = []
        for name in self.config['connections']:
            if name not in whitelistConnections:
                toBeRemoved.append(name)

        for name in toBeRemoved:
            self.config['connections'].pop(name)

        return self

    def __del__(self):
        if hasattr(self, 'config_hash') and self.config_hash in usingConnections:
            usingConnections.remove(self.config_hash)


# Transfer-related sychronization command
class SyncCommandTransfer(SyncCommand):

    def __init__(self, file_path, config_file_path, progress=None, onSave=False, disregardIgnore=False, whitelistConnections=[]):

        self.progress = progress

        # global ignore
        if disregardIgnore is False and ignore is not None and re_ignore.search(file_path) is not None:
            printMessage("file globally ignored: {" + os.path.basename(file_path) + "}", onlyVerbose=True)
            self.closed = True
            return

        SyncCommand.__init__(self, file_path, config_file_path)

        self.onSave = onSave
        self.disregardIgnore = False

        toBeRemoved = []
        for name in self.config['connections']:

            # on save
            if self.config['connections'][name]['upload_on_save'] is False and onSave is True:
                toBeRemoved.append(name)
                continue

            # ignore
            if disregardIgnore is False and self.config['connections'][name]['ignore'] is not None and re.search(self.config['connections'][name]['ignore'], file_path):
                printMessage("file ignored by rule: {" + self.basename + "}", name, True)
                toBeRemoved.append(name)
                continue

            # whitelist
            if len(whitelistConnections) > 0 and name not in whitelistConnections:
                toBeRemoved.append(name)
                continue

        for name in toBeRemoved:
            self.config['connections'].pop(name)


# Upload command
class SyncCommandUpload(SyncCommandTransfer):

    def __init__(self, file_path, config_file_path, progress=None, onSave=False, disregardIgnore=False, whitelistConnections=[]):
        SyncCommandTransfer.__init__(self, file_path, config_file_path, progress, onSave, disregardIgnore, whitelistConnections)

        self.delayed = False
        self.afterwatch = None


    def scanWatched(self, event, name, properties):
            root = os.path.dirname(self.config_file_path)
            watch = properties['after_save_watch']
            self.afterwatch[event][name] = {}

            if type(watch) is list and len(watch) > 0 and properties['upload_delay'] > 0:
                for folder, filepattern in watch:
                    self.afterwatch[event][name] = dict(self.afterwatch[event][name].items() + gatherMetafiles(filepattern, os.path.join(root, folder)).items())


    def execute(self):
        if self.progress is not None:
            self.progress.progress()

        if self.closed is True:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": command is closed")
            return

        if len(self.config['connections']) == 0:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": zero connections apply")
            return

        # afterwatch
        if self.onSave is True:
            self.afterwatch = {
                'before': {},
                'after': {}
            }

            index = -1

            for name in self.config['connections']:
                index += 1
                self.scanWatched('before', name, self.config['connections'][name])

        usingConnections.append(self.config_hash)
        stored = []
        index = -1

        for name in self.config['connections']:
            index += 1

            try:
                # identification
                connection = self.connections[index]
                id = os.urandom(32)
                scheduledUploads[self.file_path] = id

                # action
                def action():
                    try:

                        # cancelled
                        if scheduledUploads[self.file_path] != id:
                            return

                        # process
                        connection.put(self.file_path)
                        stored.append(name)
                        printMessage("uploaded {" + self.basename + "}", name)

                        # cleanup
                        scheduledUploads.pop(self.file_path)

                        if self.delayed is True:
                            # afterwatch
                            self.afterwatch['after'][name] = {}
                            self.scanWatched('after', name, self.config['connections'][name])
                            changed = getChangedFiles(self.afterwatch['before'][name], self.afterwatch['after'][name])
                            for change in changed:
                                change = change.getPath()
                                SyncCommandUpload(change, getConfigFile(change), None, False, True, [name]).execute()

                            self.delayed = False
                            self.__del__()

                        # no need to handle progress, delay action only happens with single uploads

                    except Exception, e:
                        printMessage("upload failed: {" + self.basename + "} <Exception: " + stringifyException(e) + ">", name, False, True)
                        handleException(e)


                # delayed
                if self.onSave is True and self.config['connections'][name]['upload_delay'] > 0:
                    self.delayed = True
                    printMessage("delaying upload of " + self.basename + " by " + unicode(self.config['connections'][name]['upload_delay']) + " seconds", name, onlyVerbose=True)
                    sublime.set_timeout(action, self.config['connections'][name]['upload_delay'] * 1000)
                else:
                    action()

            except IndexError:
                continue

            except EOFError:
                printMessage("Connection has been terminated, please retry your action", name, False, True)
                self._closeConnection()

            except Exception, e:
                printMessage("upload failed: {" + self.basename + "} <Exception: " + stringifyException(e) + ">", name, False, True)
                handleException(e)

        if len(stored) > 0:
            dumpMessage(getProgressMessage(stored, self.progress, "uploaded", self.basename))

    def __del__(self):
        if self.delayed is False:
            SyncCommand.__del__(self)


# Download command
class SyncCommandDownload(SyncCommandTransfer):

    def __init__(self, file_path, config_file_path, progress=None, onSave=False, disregardIgnore=False, whitelistConnections=[]):
        SyncCommandTransfer.__init__(self, file_path, config_file_path, progress, onSave, disregardIgnore, whitelistConnections)

        self.isDir = False
        self.forced = False
        self.skip = False

    def setIsDir(self):
        self.isDir = True

        return self

    def setForced(self):
        self.forced = True

        return self

    def setSkip(self):
        self.skip = True

        return self

    def execute(self):
        self.forced = True

        if self.progress is not None and self.isDir is not True:
            self.progress.progress()

        if self.closed is True:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": command is closed")
            return

        if len(self.config['connections']) == 0:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": zero connections apply")
            return

        usingConnections.append(self.config_hash)
        index = -1
        stored = []

        for name in self.config['connections']:
            index += 1

            try:
                if self.isDir or os.path.isdir(self.file_path):
                    file_path = self._localizePath(self.config['connections'][name], self.file_path)

                    contents = self.connections[index].list(file_path)

                    if os.path.exists(file_path) is False:
                        os.mkdir(file_path)

                    for entry in contents:
                        if entry.isDirectory() is False:
                            self.progress.add([entry.getName()])

                    for entry in contents:
                        full_name = os.path.join(file_path, entry.getName())

                        command = SyncCommandDownload(full_name, self.config_file_path, progress=self.progress, disregardIgnore=self.disregardIgnore)

                        if self.forced:
                            command.setForced()

                        if entry.isDirectory() is True:
                            command.setIsDir()
                        elif not self.forced and entry.isNewerThan(full_name) is True:
                            command.setSkip()

                        command.execute()

                    return

                else:
                    if not self.skip or self.forced:
                        self.connections[index].get(self.file_path)
                        printMessage("downloaded {" + self.basename + "}", name)
                    else:
                        printMessage("skipping {" + self.basename + "}", name)

                    stored.append(name)

            except IndexError:
                continue

            except EOFError:
                printMessage("Connection has been terminated, please retry your action", name, False, True)
                self._closeConnection()

            except Exception, e:
                printMessage("download of {" + self.basename + "} failed <Exception: " + stringifyException(e) + ">", name, False, True)
                handleException(e)

        if len(stored) > 0:
            dumpMessage(getProgressMessage(stored, self.progress, "downloaded", self.basename))


# Rename command
class SyncCommandRename(SyncCommand):

    def __init__(self, file_path, config_file_path, new_name):
        if isString(new_name) is False:
            printMessage("Cancelling SyncCommandRename: invalid new_name given (type: " + unicode(type(new_name)) + ")")
            self.close()
            return

        if len(new_name) == 0:
            printMessage("Cancelling SyncCommandRename: empty new_name given")
            self.close()
            return

        self.new_name = new_name
        self.dirname = os.path.dirname(file_path)
        SyncCommand.__init__(self, file_path, config_file_path)

    def execute(self):
        if self.closed is True:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": command is closed")
            return

        if len(self.config['connections']) == 0:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": zero connections apply")
            return

        usingConnections.append(self.config_hash)
        index = -1
        renamed = []

        exists = []
        remote_new_name = os.path.join( os.path.split(self.file_path)[0], self.new_name)
        for name in self.config['connections']:
            index += 1

            check = self.connections[index].list(remote_new_name)

            if type(check) is list and len(check) > 0:
                exists.append(name)

        def action(forced=False):
            index = -1

            for name in self.config['connections']:
                index += 1

                try:
                    self.connections[index].rename(self.file_path, self.new_name, forced)
                    printMessage("renamed {" + self.basename + "} -> {" + self.new_name + "}", name)
                    renamed.append(name)

                except IndexError:
                    continue

                except TargetAlreadyExists, e:
                    printMessage(stringifyException(e))

                except EOFError:
                    printMessage("Connection has been terminated, please retry your action", name, False, True)
                    self._closeConnection()

                except Exception, e:
                    printMessage("renaming failed: {" + self.basename + "} -> {" + self.new_name + "} <Exception: " + stringifyException(e) + ">", name, False, True)
                    handleException(e)

            # rename file
            os.rename(self.file_path, os.path.join(self.dirname, self.new_name))

            # message
            if len(renamed) > 0:
                printMessage("remotely renamed {" + self.basename + "} -> {" + self.new_name + "}", "remotes: " + ','.join(renamed), status=True)


        if len(exists) == 0:
            action()
        else:
            def sync(index):
                if index is 1:
                    printMessage("Renaming: overwriting target")
                    action(True)
                else:
                    printMessage("Renaming: keeping original")

            items = [
                "Such file already exists in <" + ','.join(exists) + "> - cancel rename?",
                "Overwrite target"
            ]

            sublime.set_timeout(lambda: sublime.active_window().show_quick_panel(items, sync), 1)


# Rename command
class SyncCommandGetMetadata(SyncCommand):

    def execute(self):
        if self.closed is True:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": command is closed")
            return

        if len(self.config['connections']) == 0:
            printMessage("Cancelling " + unicode(self.__class__.__name__) + ": zero connections apply")
            return

        usingConnections.append(self.config_hash)
        index = -1
        results = []

        for name in self.config['connections']:
            index += 1

            try:
                metadata = self.connections[index].list(self.file_path)

                if type(metadata) is list and len(metadata) > 0:
                    results.append({
                        'connection': name,
                        'metadata': metadata[0]
                    })

            except IndexError:
                continue

            except EOFError:
                printMessage("Connection has been terminated, please retry your action", name, False, True)
                self._closeConnection()

            except Exception, e:
                printMessage("getting metadata failed: {" + self.basename + "} <Exception: " + stringifyException(e) + ">", name, False, True)
                handleException(e)

        return results


def performRemoteCheck(file_path, window, forced=False):
    if type(file_path) is not str and type(file_path) is not unicode:
        return

    if window is None:
        return

    basename = os.path.basename(file_path)

    printMessage("Checking {" + basename + "} if up-to-date", status=True)

    config_file_path = getConfigFile(file_path)
    if config_file_path is None:
        return printMessage("Found no config > for file: " + file_path, status=forced)

    config = loadConfig(config_file_path)
    checking = []

    if forced is False:
        for name in config['connections']:
            if config['connections'][name]['download_on_open'] is True:
                checking.append(name)

        if len(checking) is 0:
            return

    try:
        metadata = SyncCommandGetMetadata(file_path, config_file_path).whitelistConnections(checking).execute()
    except Exception, e:
        printMessage("Error when getting metadata: " + stringifyException(e))
        handleException(e)
        metadata = []

    if type(metadata) is not list:
        return printMessage("Invalid metadata response, expected list, got " + unicode(type(metadata)))

    if len(metadata) == 0:
        return printMessage("No version of {" + basename + "} found on any server", status=True)

    newest = []
    oldest = []
    every = []

    for entry in metadata:
        if entry['metadata'].isNewerThan(file_path):
            newest.append(entry)
            every.append(entry)
        else:
            oldest.append(entry)

            if entry['metadata'].isDifferentSizeThan(file_path):
                every.append(entry)

    if len(every) > 0:
        every = metadata
        sorted(every, key=lambda entry: entry['metadata'].getLastModified())
        every.reverse()

        def sync(index):
            if index > 0:
                if isDebug:
                    i = 0
                    for entry in every:
                        printMessage("Listing connection " + unicode(i) + ": " + unicode(entry['connection']))
                        i += 1

                    printMessage("Index selected: " + unicode(index - 1))

                RemoteSyncDownCall(file_path, getConfigFile(file_path), True, whitelistConnections=[every[index - 1]['connection']]).start()

        filesize = os.path.getsize(file_path)
        items = ["Keep current (" + unicode(round(float(os.path.getsize(file_path)) / 1024, 3)) + " kB | " + formatTimestamp(os.path.getmtime(file_path)) + ")"]
        index = 1

        for item in every:
            item_filesize = item['metadata'].getFilesize()

            if item_filesize == filesize:
                item_filesize = "same size"
            else:
                if item_filesize > filesize:
                    item_filesize = unicode(round(item_filesize / 1024, 3)) + " kB ~ larger"
                else:
                    item_filesize = unicode(round(item_filesize / 1024, 3)) + " kB ~ smaller"

            time = unicode(item['metadata'].getLastModifiedFormatted(time_format))

            if item in newest:
                time += " ~ newer"
            else:
                time += " ~ older"

            items.append(["Get from <" + item['connection'] + "> (" + item_filesize + " | " + time + ")"])
            index += 1

        sublime.set_timeout(lambda: window.show_quick_panel(items, sync), 1)
    else:
        printMessage("All remote versions of {" + basename + "} are of same size and older", status=True)


# ==== Watching ===========================================================================

# list of file paths to be checked on load
checksScheduled = []
# pre_save x post_save upload prevention
preventUpload = []


# File watching
class RemoteSync(sublime_plugin.EventListener):

    # @todo - put into thread
    def on_pre_save(self, view):
        file_path = view.file_name()
        config_file_path = getConfigFile(file_path)
        if config_file_path is None:
            return

        config = loadConfig(config_file_path)
        blacklistConnections = []
        for connection in config['connections']:
            if config['connections'][connection]['upload_on_save'] is False:
                blacklistConnections.append(connection)


        if len(blacklistConnections) == len(config['connections']):
            return

        try:
            metadata = SyncCommandGetMetadata(file_path, config_file_path).execute()
        except Exception, e:
            if str(e).find('No such file'):
                printMessage("No version of {" + basename + "} found on any server", status=True)
            else:
                printMessage("Error when getting metadata: " + stringifyException(e))
                handleException(e)
            metadata = []

        newest = None
        newer = []
        index = 0

        for entry in metadata:
            if entry['connection'] not in blacklistConnections and config['connections'][entry['connection']]['check_time'] is True and entry['metadata'].isNewerThan(file_path):
                newer.append(entry['connection'])

                if newest is None or newest > entry['metadata'].getLastModified():
                    newest = index

            index += 1

        if len(newer) > 0:
            preventUpload.append(file_path)

            def sync(index):
                if index is 1:
                    printMessage("Overwrite prevention: overwriting")
                    self.on_post_save(view)
                else:
                    printMessage("Overwrite prevention: cancelled upload")

            items = [
                "Newer entry in <" + ','.join(newer) + "> - cancel upload?",
                "Overwrite, newest: " + metadata[newest]['metadata'].getLastModifiedFormatted()
            ]

            window = view.window()
            if window is None:
                window = sublime.active_window()  # only in main thread!

            sublime.set_timeout(lambda: window.show_quick_panel(items, sync), 1)

    def on_post_save(self, view):
        file_path = view.file_name()

        if file_path in preventUpload:
            preventUpload.remove(file_path)
            return

        RemoteSyncCall(file_path, getConfigFile(file_path), True).start()

    def on_close(self, view):
        file_path = view.file_name()

        config_file_path = getConfigFile(file_path)

        if file_path in checksScheduled:
            checksScheduled.remove(file_path)

        if config_file_path is not None:
            closeConnection(getFilepathHash(config_file_path))

    # When a file is loaded and at least 1 connection has download_on_open enabled
    # it will check those enabled if the remote version is newer and offers the newest to download
    def on_load(self, view):
        file_path = view.file_name()

        if ignore is not None and re_ignore.search(file_path) is not None:
            return

        if view not in checksScheduled:
            checksScheduled.append(file_path)

            def check():
                if file_path in checksScheduled:
                    RemoteSyncCheck(file_path, view.window()).start()

            sublime.set_timeout(check, download_on_open_delay)


# ==== Threading ===========================================================================

def fillProgress(progress, entry):
    if len(entry) == 0:
        return

    if type(entry[0]) is str or type(entry[0]) is unicode:
        entry = entry[0]

    if type(entry) is list:
        for item in entry:
            fillProgress(progress, item)
    else:
        progress.add([entry])


class RemoteSyncCall(threading.Thread):
    def __init__(self, file_path, config, onSave, disregardIgnore=False, whitelistConnections=[]):
        self.file_path = file_path
        self.config = config
        self.onSave = onSave
        self.disregardIgnore = disregardIgnore
        self.whitelistConnections = whitelistConnections
        threading.Thread.__init__(self)

    def run(self):
        target = self.file_path

        if (type(target) is str or type(target) is unicode) and self.config is None:
            return False

        elif type(target) is str or type(target) is unicode:
            SyncCommandUpload(target, self.config, onSave=self.onSave, disregardIgnore=self.disregardIgnore, whitelistConnections=self.whitelistConnections).execute()

        elif type(target) is list and len(target) > 0:
            progress = Progress()
            fillProgress(progress, target)

            for file_path, config in target:
                SyncCommandUpload(file_path, config, progress=progress, onSave=self.onSave, disregardIgnore=self.disregardIgnore, whitelistConnections=self.whitelistConnections).execute()


class RemoteSyncDownCall(threading.Thread):
    def __init__(self, file_path, config, disregardIgnore=False, forced=False, whitelistConnections=[]):
        self.file_path = file_path
        self.config = config
        self.disregardIgnore = disregardIgnore
        self.forced = forced
        self.whitelistConnections = []
        threading.Thread.__init__(self)

    def run(self):
        target = self.file_path

        if (type(target) is str or type(target) is unicode) and self.config is None:
            return False

        elif type(target) is str or type(target) is unicode:
            command = SyncCommandDownload(target, self.config, disregardIgnore=self.disregardIgnore, whitelistConnections=self.whitelistConnections)

            if self.forced:
                command.setForced()

            command.execute()
        elif type(target) is list and len(target) > 0:
            total = len(target)
            progress = Progress(total)

            for file_path, config in target:
                if os.path.isfile(file_path):
                    progress.add([file_path])

                command = SyncCommandDownload(file_path, config, disregardIgnore=self.disregardIgnore, progress=progress, whitelistConnections=self.whitelistConnections)

                if self.forced:
                    command.setForced()

                command.execute()


class RemoteSyncRename(threading.Thread):
    def __init__(self, file_path, config, new_name):
        self.file_path = file_path
        self.new_name = new_name
        self.config = config
        threading.Thread.__init__(self)

    def run(self):
        SyncCommandRename(self.file_path, self.config, self.new_name).execute()


class RemoteSyncCheck(threading.Thread):
    def __init__(self, file_path, window, forced=False):
        self.file_path = file_path
        self.window = window
        self.forced = forced
        threading.Thread.__init__(self)

    def run(self):
        performRemoteCheck(self.file_path, self.window, self.forced)


# ==== Commands ===========================================================================

# Sets up a config file in a directory
class FtpSyncNewSettings(sublime_plugin.TextCommand):
    def run(self, edit, dirs):
        if len(dirs) == 0:
            dirs = [os.path.dirname(self.view.file_name())]

        default = os.path.join(sublime.packages_path(), 'FTPSync', connectionDefaultsFilename)

        for directory in dirs:
            config = os.path.join(directory, configName)

            invalidateConfigCache(directory)

            if os.path.exists(config) is True:
                self.view.window().open_file(config)
            else:
                shutil.copyfile(default, config)
                self.view.window().open_file(config)


# Synchronize up selected file/directory
class FtpSyncTarget(sublime_plugin.TextCommand):
    def run(self, edit, paths):
        syncFiles = []
        fileNames = []

        # gather files
        for target in paths:
            if os.path.isfile(target):
                if target not in fileNames:
                    fileNames.append(target)
                    syncFiles.append([target, getConfigFile(target)])
            elif os.path.isdir(target):
                empty = True

                for root, dirs, files in os.walk(target):
                    for file_path in files:
                        empty = False

                        if file_path not in fileNames:
                            fileNames.append(target)
                            syncFiles.append([os.path.join(root, file_path), getConfigFile(os.path.join(root, file_path))])

                    for folder in dirs:
                        path = os.path.join(root, folder)

                        if not os.listdir(path) and path not in fileNames:
                            fileNames.append(path)
                            syncFiles.append([path, getConfigFile(path)])


                if empty is True:
                    syncFiles.append([target, getConfigFile(target)])

        # sync
        RemoteSyncCall(syncFiles, None, False).start()


# Synchronize up current file
class FtpSyncCurrent(sublime_plugin.TextCommand):
    def run(self, edit):
        file_path = sublime.active_window().active_view().file_name()

        RemoteSyncCall(file_path, getConfigFile(file_path), False).start()


# Synchronize down current file
class FtpSyncDownCurrent(sublime_plugin.TextCommand):
    def run(self, edit):
        file_path = sublime.active_window().active_view().file_name()

        RemoteSyncDownCall(file_path, getConfigFile(file_path), False, True).start()


# Checks whether there's a different version of the file on server
class FtpSyncCheckCurrent(sublime_plugin.TextCommand):
    def run(self, edit):
        file_path = sublime.active_window().active_view().file_name()
        view = sublime.active_window()

        RemoteSyncCheck(file_path, view, True).start()


# Synchronize down selected file/directory
class FtpSyncDownTarget(sublime_plugin.TextCommand):
    def run(self, edit, paths, forced=False):
        RemoteSyncDownCall(getFiles(paths, getConfigFile), None, forced=forced).start()


# Renames a file on disk and in folder
class FtpSyncRename(sublime_plugin.TextCommand):
    def run(self, edit, paths):
        self.original_path = paths[0]
        self.folder = os.path.dirname(self.original_path)
        self.original_name = os.path.basename(self.original_path)

        if self.original_path in checksScheduled:
            checksScheduled.remove(self.original_path)

        self.view.window().show_input_panel('Enter new name', self.original_name, self.rename, None, None)

    def rename(self, new_name):
        RemoteSyncRename(self.original_path, getConfigFile(self.original_path), new_name).start()


# Removes given file(s) or folders
class FtpSyncDelete(sublime_plugin.TextCommand):
    def run(self, edit, paths):
        pass