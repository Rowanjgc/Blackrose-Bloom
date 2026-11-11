"""Trusted standalone launcher, executed only inside the mandatory namespaces.

Install resource limits and a default-deny syscall filter before package code.
No app imports, dependencies from the package, or shell setup occur here.
"""
import ctypes
import errno
from pathlib import Path
import resource
import sys


def confine():
    for kind, limit in ((resource.RLIMIT_AS, 256 * 1024 * 1024),
                        (resource.RLIMIT_CPU, 5), (resource.RLIMIT_NOFILE, 32),
                        (resource.RLIMIT_FSIZE, 1024 * 1024), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(kind, (limit, limit))
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError('Cannot prevent privilege gain')
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise OSError('Cannot create seccomp filter')
    try:
        # No exec, clone/fork, sockets, ptrace, mount, namespaces, io_uring,
        # process_vm, seccomp/prctl, or kernel keyring operations.
        allowed = '''read write readv writev close close_range fstat newfstatat stat lstat
            statx lseek pread64 pwrite64 open openat access faccessat faccessat2 readlink
            readlinkat getdents getdents64 fcntl ioctl dup dup2 dup3
            mmap mprotect munmap mremap madvise brk
            rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack
            futex futex_time64 set_tid_address set_robust_list rseq
            clock_gettime clock_gettime64 clock_nanosleep nanosleep gettimeofday time
            getpid getppid gettid getuid geteuid getgid getegid getgroups uname
            getcwd chdir fchdir mkdir mkdirat rmdir unlink unlinkat rename renameat renameat2
            ftruncate truncate fsync fdatasync umask chmod fchmod fchmodat
            getrandom sched_getaffinity sched_yield getrlimit getrusage sysinfo
            poll ppoll select pselect6 restart_syscall exit exit_group'''.split()
        for name in allowed:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add(context, 0x7fff0000, number, 0) != 0:
                raise OSError('Cannot configure seccomp filter')
        if library.seccomp_load(context) != 0:
            raise OSError('Cannot enforce seccomp filter')
    finally:
        library.seccomp_release(context)


def main():
    try:
        confine()
    except (OSError, ValueError):
        return 78
    sys.stdout.write('BLACKROSE_SCRIPT_V1\n')
    sys.stdout.flush()
    entry = Path('/skill') / sys.argv[1]
    # -I removes ambient paths; allow imports only from the reviewed snapshot
    # and system standard library. No site startup or bytecode writes.
    sys.path.insert(0, '/skill')
    source = entry.read_bytes()
    namespace = {'__name__': '__main__', '__file__': str(entry), '__package__': None}
    exec(compile(source, str(entry), 'exec'), namespace)
    return 0


if __name__ == '__main__':
    sys.exit(main())
