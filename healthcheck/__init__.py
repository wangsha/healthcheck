import json
import os
import platform
import socket
import sys
import time
import traceback
from datetime import datetime

from flask import current_app

try:
    from functools import reduce
except Exception:
    pass


def basic_exception_handler(_, e):
    return False, str(e)


def json_success_handler(results):
    data = {
        'hostname': socket.gethostname(),
        'status': 'success',
        'timestamp': time.time(),
        'results': results,
    }

    return json.dumps(data)


def json_failed_handler(results):
    data = {
        'hostname': socket.gethostname(),
        'status': 'failure',
        'timestamp': time.time(),
        'results': results,
    }

    return json.dumps(data)


def check_reduce(passed, result):
    return passed and result.get('passed')


class HealthCheck(object):
    def __init__(self, app=None, path=None, success_status=200,
                 success_headers=None, success_handler=json_success_handler,
                 success_ttl=27, failed_status=500, failed_headers=None,
                 failed_handler=json_failed_handler, failed_ttl=9,
                 exception_handler=basic_exception_handler, checkers=None,
                 log_on_failure=True, cache_handler=None,
                 **options):
        self.cache = dict()
        self.cache_handler = cache_handler

        self.success_status = success_status
        self.success_headers = success_headers or {'Content-Type': 'application/json'}
        self.success_handler = success_handler
        self.success_ttl = float(success_ttl or 0)

        self.failed_status = failed_status
        self.failed_headers = failed_headers or {'Content-Type': 'application/json'}
        self.failed_handler = failed_handler
        self.failed_ttl = float(failed_ttl or 0)

        self.exception_handler = exception_handler

        self.log_on_failure = log_on_failure

        self.options = options
        self.checkers = checkers or []

        if app:
            self.init_app(app, path)

    def init_app(self, app, path):
        if path:
            app.add_url_rule(path, view_func=self.check, **self.options)

    def add_check(self, func):
        self.checkers.append(func)

    def check(self, *args, **kwargs):
        results = []
        if self.cache_handler:
            self.cache = self.cache_handler()
        for checker in self.checkers:
            key = checker.__name__
            if key in self.cache and self.cache[key].get('expires') >= time.time():
                result = self.cache[key]
            else:
                result = self.run_check(checker)
                self.cache[key] = result
            results.append(result)

        passed = reduce(check_reduce, results, True)

        if passed:
            message = "OK"
            if self.success_handler:
                message = self.success_handler(results)
            headers = self.success_headers() if callable(self.success_headers) else self.success_headers
            
            return message, self.success_status, headers
        else:
            message = "NOT OK"
            if self.failed_handler:
                message = self.failed_handler(results)
            headers = self.failed_headers() if callable(self.failed_headers) else self.failed_headers

            return message, self.failed_status, headers

    def run_check(self, checker):
        try:
            passed, output = checker()
        except Exception:
            traceback.print_exc()
            e = sys.exc_info()[0]
            current_app.logger.exception(e)
            passed, output = self.exception_handler(checker, e)

        if not passed:
            msg = 'Health check "{}" failed with output "{}"'.format(checker.__name__, output)
            if self.log_on_failure:
                current_app.logger.error(msg)

        timestamp = time.time()
        if passed:
            expires = timestamp + self.success_ttl
        else:
            expires = timestamp + self.failed_ttl

        result = {'checker': checker.__name__,
                  'output': output,
                  'passed': passed,
                  'timestamp': timestamp,
                  'expire_at': "%s" % datetime.fromtimestamp(expires),
                  'expires': expires}
        return result


class EnvironmentDump(object):
    def __init__(self, app=None, path=None,
                 include_os=True, include_python=True,
                 include_config=True, include_process=True, **options):
        self.functions = {}
        if include_os:
            self.functions['os'] = self.get_os
        if include_python:
            self.functions['python'] = self.get_python
        if include_config:
            self.functions['config'] = self.get_config
        if include_process:
            self.functions['process'] = self.get_process

        self.options = options
        if app:
            self.init_app(app, path)

    def init_app(self, app, path):
        if path:
            app.add_url_rule(path, view_func=self.dump_environment, **self.options)

    def add_section(self, name, func):
        if name in self.functions:
            raise Exception('The name "{}" is already taken.'.format(name))
        self.functions[name] = func

    def dump_environment(self):
        data = {}
        for (name, func) in self.functions.items():
            data[name] = func()

        return json.dumps(data), 200, {'Content-Type': 'application/json'}

    def get_os(self):
        return {'platform': sys.platform,
                'name': os.name,
                'uname': platform.uname()}

    def get_config(self):
        return self.safe_dump(current_app.config)

    def get_python(self):
        result = {'version': sys.version,
                  'executable': sys.executable,
                  'pythonpath': sys.path,
                  'version_info': {'major': sys.version_info.major,
                                   'minor': sys.version_info.minor,
                                   'micro': sys.version_info.micro,
                                   'releaselevel': sys.version_info.releaselevel,
                                   'serial': sys.version_info.serial}}
        import importlib
        import pip
        from pip._internal.utils.misc import get_installed_distributions
        if importlib.util.find_spec('pip'):
            # Get the installed distributions using the modern pip API
            installed_packages = {pkg.project_name: pkg.version for pkg in get_installed_distributions()}
            result = {}
            result['packages'] = installed_packages

        return result

    def get_login(self):
        # Based on https://github.com/gitpython-developers/GitPython/pull/43/
        # Fix for 'Inappopropirate ioctl for device' on posix systems.
        if os.name == "posix":
            import pwd
            username = pwd.getpwuid(os.geteuid()).pw_name
        else:
            username = os.environ.get('USER', os.environ.get('USERNAME', 'UNKNOWN'))
            if username == 'UNKNOWN' and hasattr(os, 'getlogin'):
                username = os.getlogin()
        return username

    def get_process(self):
        return {'argv': sys.argv,
                'cwd': os.getcwd(),
                'user': self.get_login(),
                'pid': os.getpid(),
                'environ': self.safe_dump(os.environ)}

    def safe_dump(self, dictionary):
        result = {}
        for key in dictionary.keys():
            if 'key' in key.lower() or 'token' in key.lower() or 'pass' in key.lower():
                # Try to avoid listing passwords and access tokens or keys in the output
                result[key] = "********"
            else:
                try:
                    json.dumps(dictionary[key])
                    result[key] = dictionary[key]
                except TypeError:
                    pass
        return result
