#!/usr/bin/env python3
"""Drive the CHR serial console (unix socket) for bootstrap only. stdlib only.
Usage: console.py <socket> <password> <cmd>...   (logs in as admin+ct; handles first-login prompts)"""
import re
import socket
import sys
import time

sock_path, password, cmds = sys.argv[1], sys.argv[2], sys.argv[3:]
s = socket.socket(socket.AF_UNIX); s.connect(sock_path); s.settimeout(0.5)
buf = ""
def rd(t=2.0):
    global buf
    end = time.time() + t; out = ""
    while time.time() < end:
        try:
            d = s.recv(65536).decode("utf-8", "replace")
            if d: out += d; end = time.time() + 0.8
        except TimeoutError:
            pass
    buf += out; return out
def send(x): s.sendall(x.encode())
def until(pat, t=60):
    end = time.time() + t; acc = ""
    while time.time() < end:
        acc += rd(1.0)
        if re.search(pat, acc): return acc
    raise SystemExit(f"timeout waiting for {pat!r}; got tail: {acc[-400:]!r}")

send("\x03\r"); acc = rd(3)
if "] >" not in acc:
    if "Login:" not in acc and "login:" not in acc:
        send("\r"); until(r"[Ll]ogin:")
    send("admin+ct\r"); until("Password:"); send(password + "\r")
    acc = until(r"(\] >|license|new password|Login failed)", 60)
    if "Login failed" in acc and password:  # fresh box: empty password
        send("admin+ct\r"); until("Password:"); send("\r"); acc = until(r"(\] >|license|new password)", 60)
    if "license" in acc: send("n\r"); acc = until(r"(\] >|new password)", 30)
    if "new password" in acc:
        send(password + "\r"); until("repeat new password|new password"); send(password + "\r"); until(r"\] >", 30)
for c in cmds:
    if c.startswith("!"):  # confirm-and-disconnect commands (reset-configuration, reboot)
        send(c[1:] + "\r"); rd(3); send("y"); print(rd(5).replace("\x1b", "")); sys.exit(0)
    send(c + "\r"); out = until(r"\] > $|\] >\s*$", 120)
    print(out.replace("\x1b", "").strip()); print("-----")
send("/quit\r"); rd(1)
