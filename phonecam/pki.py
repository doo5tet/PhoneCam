# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny, dependency-free certificate authority for PhoneCam.

Phones only give motion sensors to secure (https) pages. PhoneCam therefore makes its own small certificate
authority on your computer (one time), which you trust on your phone once, and then issues a server certificate
for the computer's current Wi-Fi addresses. Pure Python (RSA 2048, SHA-256, X.509 v3) so it works the same on
macOS, Windows and Linux without OpenSSL.
"""
import base64
import datetime
import hashlib
import ipaddress
import os
import secrets

# ---------------------------------------------------------------- RSA


_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]


def _is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(c):
            return c


def new_rsa_key(bits=2048, e=65537):
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        try:
            d = pow(e, -1, phi)
        except ValueError:
            continue
        n = p * q
        if n.bit_length() == bits:
            return dict(n=n, e=e, d=d, p=p, q=q, dp=d % (p - 1), dq=d % (q - 1), qi=pow(q, -1, p))


# ---------------------------------------------------------------- DER (just what X.509 needs)


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(i):
    b = i.to_bytes(max(1, (i.bit_length() + 8) // 8), "big", signed=False)
    if b[0] & 0x80:
        b = b"\x00" + b
    while len(b) > 1 and b[0] == 0 and not (b[1] & 0x80):
        b = b[1:]
    return _tlv(0x02, b)


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _set(*items):
    return _tlv(0x31, b"".join(items))


def _oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


_NULL = b"\x05\x00"


def _bits(body, unused=0):
    return _tlv(0x03, bytes([unused]) + body)


def _octets(body):
    return _tlv(0x04, body)


def _utf8(s):
    return _tlv(0x0C, s.encode("utf-8"))


def _time(dt):
    if dt.year < 2050:
        return _tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode())


def _explicit(n, body):
    return _tlv(0xA0 + n, body)


def _name(cn, org="PhoneCam"):
    return _seq(_set(_seq(_oid("2.5.4.10"), _utf8(org))), _set(_seq(_oid("2.5.4.3"), _utf8(cn))))


def _spki(key):
    rsa_pub = _seq(_int(key["n"]), _int(key["e"]))
    return _seq(_seq(_oid("1.2.840.113549.1.1.1"), _NULL), _bits(rsa_pub)), rsa_pub


def _ext(oid, value, critical=False):
    return _seq(_oid(oid), *( [b"\x01\x01\xff"] if critical else []), _octets(value))


_SHA256_ALG = _seq(_oid("1.2.840.113549.1.1.11"), _NULL)
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _sign(key, data):
    k = (key["n"].bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    s1, s2 = pow(m, key["dp"], key["p"]), pow(m, key["dq"], key["q"])  # CRT
    h = (key["qi"] * (s1 - s2)) % key["p"]
    return (s2 + h * key["q"]).to_bytes(k, "big")


def _cert(subject_cn, issuer_cn, key, issuer_key, days, exts):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    spki, _ = _spki(key)
    tbs = _seq(
        _explicit(0, _int(2)),
        _int(secrets.randbits(126) | (1 << 125)),
        _SHA256_ALG,
        _name(issuer_cn),
        _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days))),
        _name(subject_cn),
        spki,
        _explicit(3, _seq(*exts)),
    )
    return _seq(tbs, _SHA256_ALG, _bits(_sign(issuer_key, tbs)))


def _key_id(key):
    return hashlib.sha1(_spki(key)[1]).digest()


# ---------------------------------------------------------------- PEM files


def _pem(kind, der):
    b = base64.encodebytes(der).decode().replace("\n", "")
    return f"-----BEGIN {kind}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {kind}-----\n"


def key_pem(key):
    der = _seq(_int(0), *(_int(key[k]) for k in ("n", "e", "d", "p", "q", "dp", "dq", "qi")))
    return _pem("RSA PRIVATE KEY", der)


def _read_key(path):
    """Read back a key written by key_pem (PKCS#1)."""
    der = base64.b64decode("".join(l for l in open(path).read().splitlines() if not l.startswith("-----")))
    vals, i = [], 0

    def read_len(j):
        n = der[j]
        if n < 0x80:
            return n, j + 1
        c = n & 0x7F
        return int.from_bytes(der[j + 1:j + 1 + c], "big"), j + 1 + c
    assert der[0] == 0x30
    _, i = read_len(1)
    while i < len(der):
        assert der[i] == 0x02
        ln, j = read_len(i + 1)
        vals.append(int.from_bytes(der[j:j + ln], "big"))
        i = j + ln
    _, n, e, d, p, q, dp, dq, qi = vals
    return dict(n=n, e=e, d=d, p=p, q=q, dp=dp, dq=dq, qi=qi)


# ---------------------------------------------------------------- public API


def ensure_ca(folder, label):
    """Create the computer's own certificate authority once. Returns (ca_key, paths)."""
    os.makedirs(folder, exist_ok=True)
    kp, cp, der_p = (os.path.join(folder, n) for n in ("ca.key", "ca.pem", "PhoneCam-CA.cer"))
    if os.path.exists(kp) and os.path.exists(cp) and os.path.exists(der_p):
        return _read_key(kp)
    key = new_rsa_key()
    kid = _key_id(key)
    der = _cert(label, label, key, key, 3650, [
        _ext("2.5.29.19", _seq(b"\x01\x01\xff"), critical=True),          # basicConstraints CA:TRUE
        _ext("2.5.29.15", _bits(b"\x06", 1), critical=True),               # keyUsage keyCertSign, cRLSign
        _ext("2.5.29.14", _octets(kid)),                                   # subjectKeyIdentifier
    ])
    with open(kp, "w") as f:
        f.write(key_pem(key))
    try:
        os.chmod(kp, 0o600)
    except OSError:
        pass
    with open(cp, "w") as f:
        f.write(_pem("CERTIFICATE", der))
    with open(der_p, "wb") as f:
        f.write(der)
    return key


def issue_server_cert(folder, ca_label, ips, dns_names):
    """(Re)issue the https certificate for the given addresses, signed by the computer's CA."""
    ca_key = ensure_ca(folder, ca_label)
    skp = os.path.join(folder, "server.key")
    if os.path.exists(skp):
        key = _read_key(skp)
    else:
        key = new_rsa_key()
        with open(skp, "w") as f:
            f.write(key_pem(key))
        try:
            os.chmod(skp, 0o600)
        except OSError:
            pass
    san = b"".join([_tlv(0x82, n.encode("ascii")) for n in dns_names] +
                   [_tlv(0x87, ipaddress.ip_address(ip).packed) for ip in ips])
    der = _cert("PhoneCam on this computer", ca_label, key, ca_key, 397, [
        _ext("2.5.29.19", _seq(), critical=True),                          # not a CA
        _ext("2.5.29.15", _bits(b"\xa0", 5), critical=True),               # digitalSignature, keyEncipherment
        _ext("2.5.29.37", _seq(_oid("1.3.6.1.5.5.7.3.1"))),               # serverAuth
        _ext("2.5.29.17", _seq(san)),                                      # subjectAltName
        _ext("2.5.29.14", _octets(_key_id(key))),
        _ext("2.5.29.35", _seq(_tlv(0x80, _key_id(ca_key)))),             # authorityKeyIdentifier
    ])
    with open(os.path.join(folder, "server.pem"), "w") as f:
        f.write(_pem("CERTIFICATE", der))
    return os.path.join(folder, "server.pem"), skp
