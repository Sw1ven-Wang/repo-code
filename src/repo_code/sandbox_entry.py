"""Trusted, fixed Linux launcher. Never import a submitted project in this process.
Invoked before privilege drop, then execs the requested program inside chroot.
"""
import ctypes
import errno
import json
import os
import resource
import sys


def main():
    cfg=json.loads(sys.argv[1])
    if sys.platform!="linux" or os.geteuid()!=0: raise RuntimeError("isolated launcher requires Linux root")
    sec=ctypes.CDLL("libseccomp.so.2",use_errno=True)
    sec.seccomp_init.argtypes=[ctypes.c_uint32];sec.seccomp_init.restype=ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p];sec.seccomp_syscall_resolve_name.restype=ctypes.c_int
    sec.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
    sec.seccomp_load.argtypes=[ctypes.c_void_p];sec.seccomp_release.argtypes=[ctypes.c_void_p]
    ctx=sec.seccomp_init(0x7fff0000)
    if not ctx: raise RuntimeError("seccomp init failed")
    denied=["socket","connect","bind","listen","accept","accept4","ptrace","process_vm_readv","process_vm_writev","mount","umount2","pivot_root","chroot","setns","unshare","bpf","keyctl","add_key","request_key","kexec_load","init_module","finit_module","delete_module","reboot","swapon","swapoff","open_by_handle_at","name_to_handle_at","setsid","setpgid","io_uring_setup","userfaultfd"]
    for name in denied:
        n=sec.seccomp_syscall_resolve_name(name.encode())
        if n>=0 and sec.seccomp_rule_add(ctx,0x50000|errno.EPERM,n,0)!=0: raise RuntimeError("seccomp rule failed")
    os.chroot(cfg["root"]);os.chdir(cfg.get("cwd","/workspace"))
    os.setgroups([]);os.setgid(cfg["uid"]);os.setuid(cfg["uid"])
    os.umask(0o077)
    limits=[(resource.RLIMIT_CORE,0),(resource.RLIMIT_NOFILE,128),(resource.RLIMIT_NPROC,32),
            (resource.RLIMIT_FSIZE,cfg["max_output"]),(resource.RLIMIT_AS,cfg["memory_bytes"]),
            (resource.RLIMIT_CPU,cfg["cpu_seconds"])]
    for which,value in limits:resource.setrlimit(which,(value,value))
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(38,1,0,0,0)!=0:raise RuntimeError("no_new_privs failed")
    if sec.seccomp_load(ctx)!=0:raise RuntimeError("seccomp load failed")
    sec.seccomp_release(ctx)
    argv=cfg["argv"]
    os.execve(argv[0],argv,{"PATH":"/usr/bin:/bin","HOME":"/tmp","TMPDIR":"/tmp","LANG":"C.UTF-8","PYTHONNOUSERSITE":"1","PYTHONDONTWRITEBYTECODE":"1"})

if __name__=="__main__":main()
