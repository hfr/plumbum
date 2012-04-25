import sys
import os
import logging
from tempfile import TemporaryFile
from subprocess import Popen, PIPE
from contextlib import contextmanager
from plumbum.path import Path


cmd_logger = logging.getLogger("LocalCommand")

WIN32 = sys.platform == "win32"
__all__ = ["local", "BG", "FG", "ProcessExecutionError", "CommandNotFound"]


class _Workdir(Path):
    def __init__(self):
        self._dirstack = [os.getcwd()]
    def __str__(self):
        return str(self._path)
    @property
    def _path(self):
        return self._dirstack[-1]
    def __repr__(self):
        return "<Workdir %s>" % (self,)
    @contextmanager
    def __call__(self, dir): #@ReservedAssignment
        self._dirstack.append(None)
        self.chdir(dir)
        try:
            yield
        finally:
            self._dirstack.pop(-1)
            self.chdir(self._dirstack[-1])
    def __hash__(self):
        raise TypeError("Workdir can change and is unhashable")

    def getpath(self):
        return Path(str(self))
    def chdir(self, dir): #@ReservedAssignment
        os.chdir(str(dir))
        self._dirstack[-1] = Path(dir)

cwd = _Workdir()

class _Env(object):
    def __init__(self):
        self._envstack = [os.environ.copy()]
        self._update_path()
    def _update_path(self):
        self.path = [Path(p) for p in self["PATH"].split(os.path.pathsep)]
    
    @contextmanager
    def __call__(self, **kwargs):
        self._envstack.append(self._envstack[-1].copy())
        self.update(**kwargs)
        try:
            yield
        finally:
            self._update_path()
            self._envstack.pop(-1)
    def __iter__(self):
        return self._envstack[-1].iteritems()
    def __contains__(self, name):
        return name in self._envstack[-1]
    def __delitem__(self, name):
        del self._envstack[-1][name]
    def __getitem__(self, name):
        return self._envstack[-1][name]
    def __setitem__(self, name, value):
        self._envstack[-1][name] = value
        if name == "PATH":
            self._update_path()
    def update(self, *args, **kwargs):
        self._envstack[-1].update(*args, **kwargs)
        self._update_path()
    def get(self, name, default = None):
        return self._envstack[-1].get(name, default)
    def getdict(self):
        self._envstack[-1]["PATH"] = os.path.pathsep.join(str(p) for p in self.path)
        return dict((str(k), str(v)) for k, v in self._envstack[-1].items())
    def expand(self, expr):
        old = os.environ
        os.environ = self.getdict()
        output = os.path.expanduser(os.path.expandvars(expr))
        os.environ = old
        return output

    @property
    def home(self):
        if "HOME" in self:
            return Path(self["HOME"])
        elif "USERPROFILE" in self:
            return Path(self["USERPROFILE"])
        elif "HOMEPATH" in self:
            return Path(self.get("HOMEDRIVE", ""), self["HOMEPATH"])
        return None
    @home.setter
    def home(self, p):
        if "HOME" in self:
            self["HOME"] = str(p)
        elif "USERPROFILE" in self:
            self["USERPROFILE"] = str(p)
        elif "HOMEPATH" in self:
            self["HOMEPATH"] = str(p)
        else:
            self["HOME"] = str(p)
    @property
    def user(self):
        if "USER" in self:
            return self["USER"]
        elif "USERNAME" in self:
            return self["USERNAME"]
        return None

env = _Env()

class ProcessExecutionError(Exception):
    def __init__(self, cmdline, retcode, stdout, stderr):
        Exception.__init__(self, cmdline, retcode, stdout, stderr)
        self.cmdline = cmdline
        self.retcode = retcode
        self.stdout = stdout
        self.stderr = stderr
    def __str__(self):
        stdout = "\n         | ".join(self.stdout.splitlines())
        stderr = "\n         | ".join(self.stderr.splitlines())
        lines = ["Command line: %r" % (self.cmdline,), "Exit code: %s" % (self.retcode)]
        if stdout:
            lines.append("Stdout:  | %s" % (stdout,))
        if stderr:
            lines.append("Stderr:  | %s" % (stderr,))
        return "\n".join(lines)

def _run(proc, retcode):
    stdout, stderr = proc.communicate()
    if not stdout:
        stdout = ""
    if not stderr:
        stderr = ""
    if retcode is not None and proc.returncode != retcode:
        raise ProcessExecutionError(proc.cmdline, proc.returncode, stdout, stderr)
    return proc.returncode, stdout, stderr

def _make_input(data, CHUNK_SIZE = 32000):
    f = TemporaryFile()
    while data:
        chunk = data[:CHUNK_SIZE]
        f.write(chunk)
        data = data[CHUNK_SIZE:]
    f.seek(0)
    return f

class ChainableCommand(object):
    def __or__(self, other):
        return Pipeline(self, other)
    def __gt__(self, stdout_file):
        return Redirection(self, stdout_file = stdout_file)
    def __ge__(self, stderr_file):
        return Redirection(self, stderr_file = stderr_file)
    def __lt__(self, stdin_file):
        return Redirection(self, stdin_file = stdin_file)
    def __lshift__(self, data):
        return Redirection(self, stdin_file = _make_input(data))
    def __call__(self, *args, **kwargs):
        if args:
            return self.run(args, **kwargs)[1]
        else:
            return self.run(**kwargs)[1]

class Command(ChainableCommand):
    cwd = cwd
    env = env
    
    def __init__(self, executable):
        self.executable = executable
    def __str__(self):
        return str(self.executable)
    def __repr__(self):
        return "<Command %s>" % (self.executable,)
    def __getitem__(self, args):
        if not isinstance(args, tuple):
            args = (args,)
        return BoundCommand(self, args)

    def popen(self, args = (), stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        if isinstance(args, str):
            args = (args,)
        cwd = str(kwargs.pop("cwd", self.cwd))
        env = kwargs.pop("env", self.env)
        if not isinstance(env, dict):
            env = env.getdict()
        
        cmdline = [str(self.executable)] + [str(a) for a in args]
        cmd_logger.debug("Running %r, cwd = %s" % (cmdline, cwd))
        proc = Popen(cmdline, executable = str(self.executable), stdin = stdin, 
            stdout = stdout, stderr = stderr, cwd = cwd, env = env, **kwargs)
        proc.cmdline = cmdline
        return proc

    def run(self, args = (), retcode = 0, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        proc = self.popen(args, stdin = stdin, stdout = stdout, stderr = stderr, **kwargs)
        return _run(proc, retcode)

class BoundCommand(ChainableCommand):
    def __init__(self, cmd, args):
        self.cmd = cmd
        self.args = args
    def __str__(self):
        return "%s %s" % (self.cmd, " ".join(repr(a) for a in self.args))
    def __repr__(self):
        return "<BoundCommand(%r, %r)>" % (self.cmd, self.args)
    def popen(self, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        return self.cmd.popen(self.args, stdin = stdin, stdout = stdout, stderr = stderr, **kwargs)
    def run(self, retcode = 0, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        return self.cmd.run(self.args, retcode = retcode, stdin = stdin, stdout = stdout, 
            stderr = stderr, **kwargs)

class Pipeline(ChainableCommand):
    def __init__(self, srccmd, dstcmd):
        self.srccmd = srccmd
        self.dstcmd = dstcmd
    def __str__(self):
        return "(%s | %s)" % (self.srccmd, self.dstcmd)
    def __repr__(self):
        return "Pipeline(%r, %r)" % (self.srccmd, self.dstcmd)
    
    def popen(self, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        srcproc = self.srccmd.popen(stdin = stdin, stderr = PIPE, **kwargs)
        dstproc = self.dstcmd.popen(stdin = srcproc.stdout, stdout = stdout, 
            stderr = stderr, **kwargs)
        srcproc.stdout.close() # allow p1 to receive a SIGPIPE if p2 exits
        srcproc.stderr.close()
        dstproc.srcproc = srcproc
        return dstproc
    
    def run(self, retcode = 0, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs): 
        dstproc = self.popen(stdin = stdin, stdout = stdout, stderr = stderr, **kwargs)
        return _run(dstproc, retcode)

class Redirection(ChainableCommand):
    def __init__(self, cmd, stdin_file = PIPE, stdout_file = PIPE, stderr_file = PIPE):
        self.cmd = cmd
        self.stdin_file = open(stdin_file, "r") if isinstance(stdin_file, str) else stdin_file
        self.stdout_file = open(stdout_file, "w") if isinstance(stdout_file, str) else stdout_file
        self.stderr_file = open(stderr_file, "w") if isinstance(stderr_file, str) else stderr_file
    
    def __repr__(self):
        args = []
        if self.stdin_file != PIPE:
            args.append("stdin_file = %r" % (self.stdin_file,))
        if self.stdout_file != PIPE:
            args.append("stdout_file = %r" % (self.stdout_file,))
        if self.stderr_file != PIPE:
            args.append("stderr_file = %r" % (self.stderr_file,))
        return "<Redirection(%r, %s)>" % (self.cmd, ", ".join(args))
    
    def __str__(self):
        parts = [str(self.cmd)]
        if self.stdin_file != PIPE:
            parts.append("< %s" % (getattr(self.stdin_file, "name", self.stdin_file),))
        if self.stdout_file != PIPE:
            parts.append("> %s" % (getattr(self.stdout_file, "name", self.stdout_file),))
        if self.stderr_file != PIPE:
            parts.append("2> %s" % (getattr(self.stderr_file, "name", self.stderr_file),))
        return " ".join(parts)
    
    def popen(self, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        return self.cmd.popen(
            stdin = self.stdin_file if self.stdin_file != PIPE else stdin,
            stdout = self.stdout_file if self.stdout_file != PIPE else stdout,
            stderr = self.stderr_file if self.stderr_file != PIPE else stderr, 
            **kwargs)
    
    def run(self, retcode = 0, stdin = PIPE, stdout = PIPE, stderr = PIPE, **kwargs):
        return _run(self.popen(stdin = stdin, stdout = stdout, stderr = stderr, **kwargs), retcode)

class CommandNotFound(Exception):
    def __init__(self, progname, path):
        Exception.__init__(self, progname, path)
        self.progname = progname
        self.path = path

class LocalCommandNamespace(object):
    _EXTENSIONS = [""]
    if WIN32:
        _EXTENSIONS += [".exe", ".bat"]

    cwd = cwd
    env = env
    
    @classmethod
    def _which(cls, progname):
        for p in env.path:
            try:
                filelist = {n.basename : n for n in p.list()}
            except OSError:
                continue
            for ext in cls._EXTENSIONS:
                n = progname + ext
                if n in filelist:
                    return filelist[n]
        return None
    
    @classmethod
    def which(cls, progname):
        if WIN32:
            progname = progname.lower()
        for pn in [progname, progname.replace("_", "-")]:
            path = cls._which(pn)
            if path:
                return path
        raise CommandNotFound(progname, list(env.path))

    def __getitem__(self, name):
        name = str(name)
        if "/" in name or "\\" in name:
            return Command(Path(name))
        else:
            return Command(self.which(name))
    
    python = Command(sys.executable)

local = LocalCommandNamespace()

class Future(object):
    def __init__(self, proc, retcode):
        self.proc = proc
        self._expected_retcode = retcode
        self._returncode = None
        self._stdout = None
        self._stderr = None
    def __repr__(self):
        return "<Future %r (%s)>" % (self.proc.cmdline, self._returncode if self.ready() else "running",)
    def ready(self):
        return self._returncode is not None
    def wait(self):
        if self.ready():
            return
        self._returncode, self._stdout, self._stderr = _run(self.proc, self._expected_retcode)
    @property
    def stdout(self):
        self.wait()
        return self._stdout
    @property
    def stderr(self):
        self.wait()
        return self._stderr
    @property
    def returncode(self):
        self.wait()
        return self._returncode

class Executer(object):
    def __init__(self, retcode = 0):
        self.retcode = retcode
    @classmethod
    def __call__(cls, retcode):
        return cls(retcode)

#class _RUN(Executer):
#    def __rand__(self, cmd):
#        return cmd(retcode = self.retcode)
#RUN = _RUN()

class _BG(Executer):
    def __rand__(self, cmd):
        return Future(cmd.popen(), self.retcode)
BG = _BG()

class _FG(Executer):
    def __rand__(self, cmd):
        return cmd(retcode = self.retcode, stdin = None, stdout = None, stderr = None)
FG = _FG()


if __name__ == "__main__":
    ls = local["ls"]
    grep = local["grep"]
    cat = local["cat"]
    sort = local["sort"]
    sleep = local["sleep"]
    
    x = (cat << "hello world\n") > sys.stdout
    x()
    
    with local.env(FOO = 17):
        env.path.append("/lalalala")
        print local.python("-c", "import os;print os.environ.get('PATH');print os.environ.get('FOO')")
    
    with local.cwd("/"):
        (ls["-l"] > "test.txt")()
    x = (grep["v"] < "test.txt") | grep["vm"] | sort > sys.stdout
    print x
    x()
    
    f = sleep[1] & BG
    print f
    f.wait()
    print f
    
    #print cmd.nano & FG(1)
    









