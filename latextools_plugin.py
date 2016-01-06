'''
Plugin auto-discovery system intended for use in LaTeXTools package.

The easiest way to make this system aware of a plugin is to create a file with the
.latextools-plugin extension containing the plugin code. However, alternative loading
mechanisms are provided.

Configuration options:
    `plugin_paths`: in the standard user configuration.
         A list of either directories to search for plugins or paths to plugins.
         Defaults to an empty list, in which case nothing will be done.

        Paths can either be specified as absolute paths, paths relative to the
        LaTeXTools package or paths relative to the User package. Paths in the
        User package will mask paths in the LaTeXTools package. This is intended to
        emulate the behaviour of ST.

        If the default glob of *.py is unacceptable, the path can instead be specified
        as a tuple consisting of the path and the glob to use. The glob *must* be compatible
        with the Python glob module. E.g.,
        ```json
            "plugin_paths": [['latextools_plugins', '*.py3']]
        ```
        will load all .py3 files in the `latextools_plugins` subdirectory of the User package.

    `plugins_whitelist`: in the standard user configuration.
        A list of modules from the LaTeXTools directory that will be exposed to
        plugins. Defaults to ['getTeXRoot', 'kpsewhich'].

        The intention of this option is to expose a minimal set of library-esque
        functionality to plugin users that may be useful. These plugins are made
        available by mangling sys.modules, though, where possible, we will point
        at the already-loaded module rather than trying to reload it.

NB Since this doesn't rely on any plugin naming conventions, this code *will* load any
Python (.py) file found in any of the configured `plugin_paths`. It is advisable that
users keep this setting restricted to a folder that only contains LaTeXTools-related
plugins, e.g., a folder named LaTeXToolsPlugins or similar.

Plugin authors should import the LaTeXToolsPlugin class and use it as a base for any
plugins. Only subclasses of LaTeXToolsPlugin will be registered and properly treated as
plugins.

Any consumers of plugins, i.e., code in the main LaTeXTools tree, should primarily
interact with this system through the `get_plugin()` function, which provides access
to the internal plugin registry.

A quick example plugin:

    from latextools_plugin import LaTeXToolsPlugin

    class PluginSample(LaTeXToolsPlugin):
        def do_something():
            pass

And example consuming code:

    from latextools_plugin import get_plugin

    plugin = get_plugin('plugin_sample')
    # instantiate and use the plugin
    plugin().do_something()

Note that we make no assumption about how plugins are used, just how they are loaded.
It is up to the consuming code to provide a protocol for interaction, i.e., methods
that will be called, etc.

The plugin environment will be setup so that the directory containing the plugin
is the first entry on sys.path, enabling import of any local modules. In addition,
a select number of modules, configured through the `plugins_whitelist` configuration
option will be exposed.
'''
from __future__ import print_function

import sublime

import glob as _glob
import os
import sys
import re

import traceback

from contextlib import contextmanager

from collections import MutableMapping

try:
    from latextools_utils import get_setting
except ImportError:
    from .latextools_utils import get_setting

if sys.version_info < (3, 0):
    exec("""def reraise(tp, value, tb=None):
    raise tp, value, tb
""")

    import imp

    def _load_module(module_name, filename, *paths):
        name, ext = os.path.splitext(filename)

        if ext in ('.py', ''):
            f, path, description = imp.find_module(name, list(paths))
            try:
                module = imp.load_module(module_name, f, path, description)
            finally:
                if f:
                    f.close()
        else:
            module = None
            exc_info = None

            for path in paths:
                p = os.path.normpath(os.path.join(path, filename))
                if os.path.exists(p):
                    try:
                        module = imp.load_source(module_name, filename)
                    except ImportError:
                        exc_info = sys.exc_info()
            if not module and exc_info:
                reraise(*exc_info)

        return module

    strbase = basestring
    FileNotFoundError = IOError
else:
    from importlib.machinery import PathFinder, SourceFileLoader
    from imp import reload

    # WARNING:
    # imp module is deprecated in 3.x, unfortunately, importlib does not seem
    # to have a stable API, as in 3.4, `find_module` is deprecated in favour of
    # `find_spec` and discussions of how best to provide access to the import
    # internals seem to be on-going
    def _load_module(module_name, filename, *paths):
        name, ext = os.path.splitext(filename)

        if ext in ('.py', ''):
            loader = PathFinder.find_module(name, path=paths)
            if loader is None:
                loader = PathFinder.find_module(name)
            if loader is None:
                raise ImportError('Could not find module {} on path {} or sys.path'.format(
                    name, paths))
        else:
            loader = None
            for path in paths:
                p = os.path.normpath(os.path.join(path, filename))
                if os.path.exists(p):
                    loader = SourceFileLoader(module_name, p)

            if loader is None:
                raise ImportError('Could not find module {} on path {}'.format(name, paths))

        loader.name = module_name
        return loader.load_module()

    strbase = str

if sublime.version() < '3000':
    import latextools_plugin_internal as internal

    def _get_sublime_module_name(_, module):
        return module
else:
    from . import latextools_plugin_internal as internal

    def _get_sublime_module_name(directory, module):
        return '{0}.{1}'.format(os.path.basename(directory), module)

__all__ = [
    'LaTeXToolsPlugin', 'get_plugin', 'add_plugin_path',
    'LaTeXToolsPluginException', 'InvalidPluginException',
    'NoSuchPluginException'
]

_MODULE_PREFIX = '_latextools_'

class LaTeXToolsPluginException(Exception):
    '''
    Base class for plugin-related exceptions
    '''
    pass

class NoSuchPluginException(LaTeXToolsPluginException):
    '''
    Exception raised if an attempt is made to access a plugin that does not exist

    Intended to allow the consumer to provide the user with some more useful information
    e.g., how to properly configure a module for an extension point
    '''
    pass

class InvalidPluginException(LaTeXToolsPluginException):
    '''
    Exception raised if an attempt is made to register a plugin that is not a
    subclass of LaTeXToolsPlugin.
    '''
    pass

class LaTeXToolsPluginRegistry(MutableMapping):
    def __init__(self):
        self._registry = {}

    def __getitem__(self, key):
        try:
            return self._registry[key]
        except KeyError:
            raise NoSuchPluginException(
                'Plugin {0} does not exist. Please ensure that the plugin is configured as documented'.format(key))

    def __setitem__(self, key, value):
        if not isinstance(value, LaTeXToolsPluginMeta):
            raise InvalidPluginException(value)

        self._registry[key] = value

    def __delitem__(self, key):
        del self._registry[key]

    def __iter__(self):
        return iter(self._registry)

    def __len__(self):
        return len(self._registry)

    def __str__(self):
        return str(self._registry)

def _classname_to_internal_name(s):
    '''
    Converts a Python class name in to an internal name

    The intention here is to mirror how ST treats *Command objects, i.e., by
    converting them from CamelCase to under_scored. Similarly, we will chop "Plugin"
    off the end of the plugin, though it isn't necessary for the class to be treated
    as a plugin.

    E.g.,
        SomeClass will become some_class
        ReferencesPlugin will become references
        BibLaTeXPlugin will become biblatex
    '''
    if not s:
        return s

    # little hack to support LaTeX or TeX in the plugin name
    while True:
        match = re.search(r'(?:La)?TeX', s)
        if match:
            s = s.replace(match.group(0), match.group(0).lower())
        else:
            break

    # pilfered from http://code.activestate.com/recipes/66009/
    s = re.sub(r'(?<=[a-z])[A-Z]|(?<!^)[A-Z](?=[a-z])', r"_\g<0>", s).lower()

    if s.endswith('_plugin'):
        s = s[:-7]

    return s

class LaTeXToolsPluginMeta(type):
    '''
    Metaclass for plugins which will automatically register them with the
    plugin registry
    '''
    def __init__(cls, name, bases, attrs):
        try:
            super(LaTeXToolsPluginMeta, cls).__init__(name, bases, attrs)
        except TypeError:
            # occurs on reload
            return

        if cls == LaTeXToolsPluginMeta:
            return

        try:
            if not any((True for base in bases if issubclass(base, LaTeXToolsPlugin))):
                return
        except NameError:
            return

        registered_name = _classname_to_internal_name(name)
        internal._REGISTRY[registered_name] = cls

LaTeXToolsPlugin = LaTeXToolsPluginMeta('LaTeXToolsPlugin', (object,), {})
LaTeXToolsPlugin.__doc__ = '''
Base class for LaTeXTools plugins. Implementation details will depend on where this
plugin is supposed to be loaded. See the documentation for details.
'''

def _get_plugin_paths():
    plugin_paths = get_setting('plugin_paths', [])
    return plugin_paths

def _load_plugin(filename, *paths):
    name, ext = os.path.splitext(filename)

    # hopefully a unique-enough module name!
    if not ext or ext == '.py':
        module_name = '{0}{1}'.format(_MODULE_PREFIX, name)
    else:
        module_name = '{0}{1}_{2}'.format(_MODULE_PREFIX, name, ext[1:])

    if module_name in sys.modules:
        try:
            reload(sys.modules[module_name])
            return sys.modules[module_name]
        except ImportError:
            # On ST2, this appears to be triggered on the initial reload and 
            # fails, so instead of reloading just continue to run the loading
            # code
            pass
        except FileNotFoundError:
            # A previous plugin has been moved or removed, so just reload it
            pass

    try:
        return _load_module(module_name, filename, *paths)
    except:
        print('Could not load module {0} using path {1}.'.format(name, paths))
        traceback.print_exc()

    return None

def _load_plugins():
    def _resolve_plugin_path(path):
        if not os.path.isabs(path):
            p = os.path.normpath(
                os.path.join(sublime.packages_path(), 'User', path))
            if not os.path.exists(p):
                p = os.path.normpath(
                    os.path.join(sublime.packages_path(), 'LaTeXTools', path))
            return p
        return path

    for path in _get_plugin_paths():
        if type(path) == strbase:
            add_plugin_path(_resolve_plugin_path(path))
        else:
            try:
                # assume path is a tuple of [<path>, <glob>]
                add_plugin_path(_resolve_plugin_path(path[0]), path[1])
            except:
                print('An error occurred while trying to add the plugin path {0}'.format(path))
                traceback.print_exc()

def get_plugin(name):
    '''
    This is intended to be the main entry-point used by consumers (not implementors)
    of plugins, to find any plugins that have registered themselves by name.

    If a plugin cannot be found, a NoSuchPluginException will be thrown. Please try
    to provide the user with any helpful information.

    Use case:
        Provide the user with the ability to load a plugin by a memorable name,
        e.g., in a settings file.

        For example, 'biblatex' will get the plugin named 'BibLaTeX', etc.
    '''
    if internal._REGISTRY is None:
        raise NoSuchPluginException('Could not load plugin {} because the registry either hasn\'t been loaded or has just been unloaded.')
    return internal._REGISTRY[name]

@contextmanager
def _latextools_module_hack():
    '''
    Context manager to ensure sys.modules has certain white-listed modules, most
    especially latextools_plugins. This exposes some of the modules in LaTeXTools
    to plugins. It is intended primarily to expose library-esque functionality,
    such as the getTeXRoot module, but can be configured by the user as-needed.
    '''
    # add any white-listed plugins to sys.modules under their own name
    plugins_whitelist = get_setting('plugins_whitelist',
        ['getTeXRoot', 'kpsewhich', 'latextools_utils']
    )

    # always include latextools_pluing
    plugins_whitelist.append('latextools_plugin')
    overwritten_modules = {}

    whitelist = [(name, None) for name in plugins_whitelist]
    whitelist.extend(internal._WHITELIST_ADDED)

    # put the directory containing this file on the sys.path
    __dir__ = os.path.dirname(__file__)

    # handles ST2s relative directory
    if __dir__ == '.':
        __dir__ = os.path.join(sublime.packages_path(), 'LaTeXTools')

    # insert the LaTeXTools directory on the path
    sys.path.insert(0, __dir__)
    for name, module in whitelist:
        if callable(module):
            module = module()

        if name in sys.modules:
            overwritten_modules[name] = sys.modules[name]

        # attempting to autoload module
        if module is None:
            # if the module has already been loaded by ST, we just use that
            latextools_module_name = _get_sublime_module_name(__dir__, name)
            if latextools_module_name in sys.modules:
                sys.modules[name] = sys.modules[latextools_module_name]
            else:
                try:
                    sys.modules[name] = _load_module(name, name, __dir__)
                except ImportError:
                    print('An error occurred while trying to load white-listed module {0}'.format(name))
                    traceback.print_exc()
        else:
            sys.modules[name] = module

    # remove the LaTeXTools directory from the path
    sys.path.pop(0)

    yield

    # restore any temporarily overwritten modules and clear our loaded modules
    for module in plugins_whitelist:
        if _get_sublime_module_name(__dir__, module) != module:
            sys.modules[module] = None
        if module in overwritten_modules:
            sys.modules[module] = overwritten_modules[module]

def add_whitelist_module(name, module=None):
    '''
    API function to ensure that a certain module is made available to any plugins.

    `name` should be the name of the module as it will be imported in a plugin
    `module`, if specified, should be either an actual module object or a callable
    that returns the actual module object.

    The `module` mechanism is provided to allow for the import of modules that
    might otherwise be unavailable or available in sys.modules only by a
    different name. Standard LaTeXTools modules should provide a name only.

    Note that this function *must* be called before add_plugin_path.
    '''
    for i, (_name, _module) in enumerate(internal._WHITELIST_ADDED):
        if _name == name:
            if _module == module:
                return
            internal._WHITELIST_ADDED[i] = (_name, module)
            return

    internal._WHITELIST_ADDED.append((name, module))

def add_plugin_path(path, glob='*.py'):
    '''
    This function adds plugins from a specified path.

    It is primarily intended to be used by consumers to load plugins from a custom
    path without needing to access the internals. For example, consuming code could
    use this to load any default plugins

    `glob`, if specified should be a valid Python glob. See the `glob` module.
    '''
    if (path, glob) not in internal._REGISTERED_PATHS_TO_LOAD:
        internal._REGISTERED_PATHS_TO_LOAD.append((path, glob))

    # if we are called before `plugin_loaded`
    if internal._REGISTRY is None:
        return

    previous_plugins = set(internal._REGISTRY.keys())

    with _latextools_module_hack():
        if not os.path.exists(path):
            return

        if os.path.isfile(path):
            plugin_dir = os.path.dirname(path)
            sys.path.insert(0, plugin_dir)

            _load_plugin(os.path.basename(path), plugin_dir)

            sys.path.pop(0)
        else:
            for file in _glob.iglob(os.path.join(path, glob)):
                plugin_dir = os.path.dirname(file)
                sys.path.insert(0, plugin_dir)

                _load_plugin(os.path.basename(file), plugin_dir)

                sys.path.pop(0)

    print('Loaded LaTeXTools plugins {0} from path {1}'.format(
        list(set(internal._REGISTRY.keys()) - previous_plugins),
        path))

# load plugins when the Sublime API is available, just in case...
def plugin_loaded():
    internal._REGISTRY = LaTeXToolsPluginRegistry()

    print('Loading LaTeXTools plugins...')
    _load_plugins()

    for path, glob in internal._REGISTERED_PATHS_TO_LOAD:
        add_plugin_path(path, glob)

    # by default load anything in the User package with a .latextools-plugin extension
    add_plugin_path(os.path.join(sublime.packages_path(), 'User'), '*.latextools-plugin')

# when this plugin is unloaded, remove the registry
def plugin_unloaded():
    internal._REGISTRY = None

# ensure plugin_loaded() called on ST2
if sublime.version() < '3000':
    unload_handler = plugin_unloaded

    if internal._REGISTRY is None:
        plugin_loaded()