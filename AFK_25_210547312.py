"""
AFK_25_210547312.py - 摩尔庄园挂机脚本 (挂机后自动领取在线奖励, 支持本地与 GitHub Actions)
"""
import os
import socket
import hashlib
import struct
import time
import sys
import re
import signal
import atexit
import argparse
import urllib.request
import select
import traceback

# 在 Linux (如 GitHub Actions) 下强制使用北京时间 (UTC+8)
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# ===================== 日志与运行状态记录 =====================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "afk.log")


class _TeeLogger:
    """将所有 print 输出实时同步写入控制台与 afk.log 文件"""
    def __init__(self, stream, log_path: str):
        self._stream = stream
        self._log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        self.encoding = "utf-8"

    def write(self, data: str):
        if self._stream is not None:
            try:
                self._stream.write(data)
                self._stream.flush()
            except Exception:
                pass
        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            pass

    def flush(self):
        if self._stream is not None:
            try:
                self._stream.flush()
            except Exception:
                pass
        try:
            self._log_file.flush()
        except Exception:
            pass


sys.stdout = _TeeLogger(sys.stdout, LOG_FILE)
sys.stderr = _TeeLogger(sys.stderr, LOG_FILE)

_RUN_STATE = {
    "start_ts": time.time(),
    "start_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    "afk_seconds": 0.0,
    "status": "启动初始化中",
    "summary_written": False,
}


def _write_exit_summary():
    if _RUN_STATE["summary_written"]:
        return
    _RUN_STATE["summary_written"] = True
    end_ts = time.time()
    end_str = time.strftime("%Y-%m-%d %H:%M:%S")
    total_sec = max(0.0, end_ts - _RUN_STATE["start_ts"])
    afk_sec = _RUN_STATE["afk_seconds"]
    print(
        f"\n{'='*50}\n"
        f"  [运行结束记录]\n"
        f"  启动时间: {_RUN_STATE['start_str']}\n"
        f"  关闭时间: {end_str}\n"
        f"  总运行时长: {total_sec/60:.1f} 分钟 ({int(total_sec)} 秒)\n"
        f"  累计挂机时长: {afk_sec/60:.1f} 分钟 ({int(afk_sec)} 秒)\n"
        f"  结束原因: {_RUN_STATE['status']}\n"
        f"  日志文件: {LOG_FILE}\n"
        f"{'='*50}\n"
    )


atexit.register(_write_exit_summary)


def _handle_signal(signum, _frame):
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    _RUN_STATE["status"] = f"被外部信号终止 ({sig_name}, 如关闭窗口/平台超时强杀)"
    _write_exit_summary()
    os._exit(0)


for _sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
    _sig = getattr(signal, _sig_name, None)
    if _sig is not None:
        try:
            signal.signal(_sig, _handle_signal)
        except Exception:
            pass


# 禁用系统代理自动检测 (避免 Windows 下 urllib 首次调用卡顿 5~15 秒)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ===================== 配置 =====================
def _load_credentials() -> tuple:
    """
    优先从环境变量 (GitHub Actions Secrets: MOLE_USER_ID / MOLE_PASSWORD) 读取账号密码;
    若未设置环境变量, 则尝试从同目录本地文件 mole_secret.txt 读取密码 (方便本地直接双击运行且避免源码泄露密码)
    """
    uid_str = os.environ.get("MOLE_USER_ID", "").strip()
    user_id = int(uid_str) if uid_str.isdigit() else 210547312

    pwd = os.environ.get("MOLE_PASSWORD", "").strip()
    if not pwd:
        secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mole_secret.txt")
        if os.path.isfile(secret_file):
            try:
                with open(secret_file, "r", encoding="utf-8") as f:
                    pwd = f.read().strip()
            except Exception:
                pass
    return user_id, pwd


USER_ID, PASSWORD = _load_credentials()
SERVER_XML_URL = "http://mole.61.com/config/Server.xml"
LOGIN_SERVER_URL = "http://login.mole.61.com/ip.txt"
LOGIN_SERVER_HOST = "login.mole.61.com"
FALLBACK_IPS = ["123.206.131.236", "123.150.163.99", "125.39.236.145"]
FALLBACK_PORT = 1863

HEADER_LEN = 17
AFK_DURATION = 95 * 60
HEARTBEAT_INTERVAL = 30
MAX_RECONNECT_ATTEMPTS = 5

# ===================== 命令常量 =====================
CMD_LOGIN = 103
CMD_GET_GOOD_SERVER_LIST = 105
CMD_ENTER_GAME = 201
CMD_ENTER_GAME_ROOM = 403
CMD_CONNECT_GAME_SERVER = 10016
CMD_CHECK_GAME_SER = 30001

# 奖励领取相关命令
CMD_ACTIVITY_OPEN = 9245   # 打开"庄园短期活动"面板
CMD_CLAIM_REWARD = 1243    # 领取单个阶段奖励
CMD_CLAIM_FINALIZE = 10101 # 领取完成确认

# 5 个阶段 ID (1min, 5min, 30min, 60min, 90min)
REWARD_STAGE_IDS = [61, 62, 63, 64, 65]

# ===================== 加密相关 =====================
L__2E_STR5 = bytes.fromhex("5e465374782c776c364e7175415652464066253600")
STATIC_TAIL = bytes.fromhex(
    "c8acc406c6cbcb688a0e8fe58ecdc629ae2ec84aca08"
    "c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08"
    "c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08"
    "c80c"
)

# CMD=9245 加密 body (从 getreward.pcapng 提取, 605 bytes 静态内容)
CMD_9245_BODY = bytes.fromhex(
    "c3cb680aaed3723eb175dc529d1dd639db54da735ad1decb688a0e8fe58ecdc6"
    "29ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc4"
    "06c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58e"
    "cdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8"
    "acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8f"
    "e58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca"
    "08c8acc406c0cbcb688a0e8fc573fd34d5da1fd57a9a14581fb8b292d77b9b16"
    "9a98e58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec8"
    "4aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb"
    "688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae"
    "2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0"
    "cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc6"
    "29ae2ec84aca08c8acc406c0cbcb688a0e8fe58e6d5a3a5b723ffd76dc7af0f5"
    "307d981fd479d9b3943c51d139ae2ec84aca08c8acc406c0cbcb688a0e8fe58e"
    "cdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8"
    "acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8f"
    "e58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca"
    "08c8acc406c0cbcb688a0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a"
    "0e8fe58ecdc629ae2ec84aca08c8acc406c0cbcb688a0e8fe58ecdc609"
)


def m_encrypt(data_in: bytes) -> bytes:
    in_len = len(data_in)
    key = L__2E_STR5
    out = bytearray(in_len + 1)
    counter = 0
    for i in range(in_len):
        if counter != 21:
            k = key[counter]; counter += 1
        else:
            k = key[0]; counter = 0
        out[i] = data_in[i] ^ k
    out[in_len] = 0
    pos = in_len
    for _ in range(in_len):
        a = out[pos - 1] & 0xFF
        b = out[pos] & 0xFF
        out[pos] = ((a >> 3) | b) & 0xFF
        out[pos - 1] = ((a << 5) & 0xFF)
        pos -= 1
    out[0] = (out[0] | 3) & 0xFF
    return bytes(out)


def m_decrypt(data_in: bytes) -> bytes:
    """Reverse MEncrypt: N+1 bytes -> N bytes"""
    data = bytearray(data_in)
    n = len(data) - 1
    data[0] &= 0xFC
    out = bytearray(n)
    for i in range(1, n + 1):
        old_val = ((data[i] & 0x1F) << 3) | (data[i - 1] >> 5)
        out[i - 1] = old_val
    key = L__2E_STR5
    counter = 0
    for i in range(n):
        if counter != 21:
            k = key[counter]; counter += 1
        else:
            k = key[0]; counter = 0
        out[i] ^= k
    return bytes(out)


# 解密得到 CMD=9245 的明文 body (静态内容"庄园短期活动" + padding)
CMD_9245_PLAIN = m_decrypt(CMD_9245_BODY)


# ===================== 协议函数 =====================
def version_event(cmd_id: int, pkg_len: int, seqno: int, body_plain: bytes) -> int:
    if cmd_id == 201:
        return seqno
    if cmd_id == 999:
        return 0
    crc = 0
    for b in body_plain:
        crc = (crc ^ b) & 0xFF
    return (seqno - seqno // 7 + 147 + pkg_len % 21 + cmd_id % 13 + crc) % 256


def build_packet(cmd: int, user_id: int, body: bytes, version: int = 1) -> bytes:
    total_len = HEADER_LEN + len(body)
    header = struct.pack('>I', total_len)
    header += struct.pack('B', version)
    header += struct.pack('>I', cmd)
    header += struct.pack('>I', user_id)
    header += struct.pack('>i', 0)
    return header + body


def build_encrypted_packet(cmd: int, user_id: int, body_plain: bytes, seq: int) -> tuple:
    """构建加密包, 返回 (packet, new_seq)"""
    pkg_len_plain = HEADER_LEN + len(body_plain)
    new_seq = version_event(cmd, pkg_len_plain, seq, body_plain)
    body_enc = m_encrypt(body_plain)
    total_len = HEADER_LEN + len(body_enc)
    packet = struct.pack('>IBIIi', total_len, new_seq, cmd, user_id, 0) + body_enc
    return packet, new_seq


def build_raw_201_packet(user_id: int, session: bytes, server_id: int,
                          version_str: bytes, login_type: int = 0) -> bytes:
    body = version_str
    body += struct.pack('>I', len(session))
    body += session
    body += struct.pack('>I', login_type)
    pkg_len = 15 + len(body)
    packet = struct.pack('>I', pkg_len)
    packet += struct.pack('B', 1)
    packet += struct.pack('>I', CMD_ENTER_GAME)
    packet += struct.pack('>I', user_id)
    packet += struct.pack('>i', 0)
    packet += struct.pack('>H', server_id)
    packet += body
    return packet


def build_encrypted_201_packet(user_id: int, session: bytes, version_str: bytes) -> bytes:
    ver_with_prefix = b'I' + version_str
    plaintext = b'\x00' + ver_with_prefix + struct.pack('>I', len(session)) + session
    return build_packet(CMD_ENTER_GAME, user_id, m_encrypt(plaintext) + STATIC_TAIL, version=65)


def enable_keepalive(sock: socket.socket):
    """启用 TCP Keep-Alive 防止长连接静默断开 (兼容 Windows 与 Linux/GitHub Actions)"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'SIO_KEEPALIVE_VALS'):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 10000))
        else:
            if hasattr(socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            if hasattr(socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, 'TCP_KEEPCNT'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except Exception:
        pass


def recv_exact(sock: socket.socket, n: int, timeout: float = 8) -> bytes:
    sock.settimeout(timeout)
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("连接中断")
        data += chunk
    return data


def parse_header(data: bytes) -> dict:
    tl, ver, cmd, uid, res = struct.unpack('>IBIIi', data[:17])
    return {'total_len': tl, 'version': ver, 'cmd': cmd, 'user_id': uid, 'result': res}


def calc_version_string(session: bytes) -> bytes:
    start1 = struct.unpack('>I', session[3:7])[0]
    start2 = struct.unpack('>I', session[10:14])[0]
    key_str = "fREd hAo crAzy BAby in Our ProgRAm?"
    md5_input = str(start2) + key_str[5:16] + str(start1)
    md5_result = hashlib.md5(md5_input.encode('utf-8')).hexdigest()
    return md5_result[6:22].encode('ascii')


def read_all_available(sock: socket.socket, timeout: float = 0.4) -> list:
    """
    带总时长上限的非阻塞收包:
    收到首个回包后仅短暂轮询 (0.05s) 清空缓冲区, 防止被广播包持续阻塞或空等超时
    """
    packets = []
    buffer = b''
    deadline = time.time() + timeout

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        wait_time = min(remaining, 0.05) if len(buffer) >= HEADER_LEN else remaining
        ready, _, _ = select.select([sock], [], [], max(0.0, wait_time))
        if not ready:
            break
        try:
            sock.settimeout(0.5)
            chunk = sock.recv(8192)
            if not chunk:
                break
            buffer += chunk
        except (socket.timeout, BlockingIOError):
            break

    while len(buffer) >= HEADER_LEN:
        h = parse_header(buffer[:HEADER_LEN])
        total = h['total_len']
        if total < HEADER_LEN or total > 65536:
            buffer = b''
            break
        if len(buffer) < total:
            try:
                buffer += recv_exact(sock, total - len(buffer), timeout=1.0)
            except Exception:
                break
        body = buffer[HEADER_LEN:total]
        packets.append({'header': h, 'body': body})
        buffer = buffer[total:]
    return packets


def _iter_login_candidates():
    """
    惰性生成登录服务器候选地址 (一旦前序地址连接成功, 绝不触发后续慢速请求):
    1. 官方客户端配置 http://mole.61.com/config/Server.xml (<DirSer ip="..." port="..." />)
    2. 域名解析 login.mole.61.com:1863
    3. 本地 FALLBACK_IPS 首选地址
    4. 旧接口 http://login.mole.61.com/ip.txt (目前 502, 仅当前序全部失败时才尝试)
    """
    try:
        with _NO_PROXY_OPENER.open(SERVER_XML_URL, timeout=2.0) as resp:
            xml_text = resp.read().decode('utf-8', errors='ignore')
            m = re.search(r'<DirSer\s+ip="([^"]+)"\s+port="(\d+)"', xml_text, re.I)
            if m:
                yield m.group(1).strip(), int(m.group(2)), "Server.xml"
    except Exception:
        pass

    try:
        _, _, ips = socket.gethostbyname_ex(LOGIN_SERVER_HOST)
        for ip in ips:
            yield ip, FALLBACK_PORT, f"DNS({LOGIN_SERVER_HOST})"
    except Exception:
        pass

    for ip in FALLBACK_IPS:
        yield ip, FALLBACK_PORT, "FALLBACK_IPS"

    try:
        with _NO_PROXY_OPENER.open(LOGIN_SERVER_URL, timeout=2.0) as resp:
            data = resp.read().decode('utf-8', errors='ignore').strip().split(':')
            if len(data) == 2 and data[1].isdigit():
                yield data[0].strip(), int(data[1]), "ip.txt"
    except Exception:
        pass


def connect_login_server() -> tuple:
    """
    短路方式连接第一个可用的登录服务器并直接返回 socket,
    避免预检空连导致的二次握手延迟。
    返回 (sock, ip, port)
    """
    seen = set()
    for ip, port, source in _iter_login_candidates():
        if (ip, port) in seen:
            continue
        seen.add((ip, port))
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            sock.connect((ip, port))
            sock.settimeout(8.0)
            print(f"[1] 已连接登录服务器 {ip}:{port} (来源: {source})")
            return sock, ip, port
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            continue

    raise RuntimeError("无法连接任何登录服务器地址")


# ===================== 初始化命令序列 =====================
INIT_COMMANDS = [
    (11010, b'', "CLI_PROTO_INIT_PLAYER_EX"),
    (3106, b'\x00\x00\x00\x00', "QUERY_SYSTEM_INFO"),
    (406, b'\x00\x00\x00\x0a\x00\x00\x00\x00', "listMapMsg(mapID=10)"),
]


def run_init_sequence(game_sock: socket.socket, user_id: int, initial_seq: int) -> int:
    seq = initial_seq
    total = len(INIT_COMMANDS)
    print(f"\n{'='*50}")
    print(f"  开始初始化协议交互 ({total} 条命令)")
    print(f"  初始 _seq = {seq}")
    print(f"{'='*50}\n")

    for i, (cmd, body_plain, desc) in enumerate(INIT_COMMANDS):
        pkt, seq = build_encrypted_packet(cmd, user_id, body_plain, seq)
        game_sock.sendall(pkt)
        body_display = body_plain.hex() if body_plain else "(empty)"
        print(f"  [{i+1}/{total}] CMD={cmd} {desc} | body={body_display} | ver={seq}")
        time.sleep(0.1)
        responses = read_all_available(game_sock, timeout=0.35)
        for r in responses:
            h = r['header']
            bp = r['body'].hex()[:40] if len(r['body']) > 0 else "(empty)"
            print(f"    <- CMD={h['cmd']} result={h['result']} body_len={len(r['body'])} body={bp}")

    late = read_all_available(game_sock, timeout=0.15)
    if late:
        for r in late:
            h = r['header']
            print(f"    <- [late] CMD={h['cmd']} result={h['result']}")
    print(f"\n  初始化完成! 最终 _seq = {seq}")
    return seq


# ===================== 挂机主循环 =====================
def afk_loop(game_sock: socket.socket, user_id: int, seq: int,
             target_duration: float, prior_elapsed: float = 0.0):
    """
    返回 (sock, final_seq, total_elapsed, completed)
    支持断线后累加已挂机时长
    """
    _RUN_STATE["status"] = "挂机进行中"
    session_start = time.time()
    last_hb = 0.0
    hb_count = 0
    last_status = -60

    remain_init = max(0.0, target_duration - prior_elapsed)
    dur_str = (f"{int(remain_init) // 3600} 小时 {(int(remain_init) % 3600) // 60} 分钟"
               if remain_init >= 3600
               else f"{int(remain_init) // 60} 分钟" if remain_init >= 60
               else f"{int(remain_init)} 秒")
    print(f"\n{'='*50}")
    print(f"  已进入挂机模式")
    if prior_elapsed > 0:
        print(f"  累计已挂机: {prior_elapsed/60:.1f} 分钟")
    print(f"  本次剩余时长: {dur_str}")
    print(f"  心跳间隔: {HEARTBEAT_INTERVAL} 秒")
    print(f"  开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")

    while True:
        session_elapsed = time.time() - session_start
        total_elapsed = prior_elapsed + session_elapsed
        _RUN_STATE["afk_seconds"] = total_elapsed
        remaining = target_duration - total_elapsed

        if remaining <= 0:
            print(f"\n  [{time.strftime('%Y-%m-%d %H:%M:%S')}] 挂机完成！累计在线 {total_elapsed/60:.1f} 分钟, 本次心跳 x{hb_count}")
            return game_sock, seq, total_elapsed, True

        if int(session_elapsed) - last_status >= 60:
            last_status = int(session_elapsed)
            mins = int(total_elapsed) // 60
            remain_mins = int(remaining) // 60
            print(f"  [{time.strftime('%Y-%m-%d %H:%M:%S')}] 累计已挂机 {mins} 分 | "
                  f"剩余 {remain_mins} 分 | 心跳 x{hb_count}")

        if session_elapsed - last_hb >= HEARTBEAT_INTERVAL:
            try:
                hb_pkt, seq = build_encrypted_packet(10301, user_id, b'', seq)
                game_sock.sendall(hb_pkt)
                hb_count += 1
                last_hb = session_elapsed
            except Exception as e:
                print(f"\n  [{time.strftime('%Y-%m-%d %H:%M:%S')}] [断线] 心跳发送失败: {e}, 累计已挂机 {total_elapsed/60:.1f} 分钟")
                return None, seq, total_elapsed, False

        try:
            ready = select.select([game_sock], [], [], 1.0)
            if ready[0]:
                data = game_sock.recv(8192)
                if not data:
                    total_elapsed = prior_elapsed + (time.time() - session_start)
                    _RUN_STATE["afk_seconds"] = total_elapsed
                    print(f"\n  [{time.strftime('%Y-%m-%d %H:%M:%S')}] [断线] 服务器关闭连接, 累计已挂机 {total_elapsed/60:.1f} 分钟")
                    return None, seq, total_elapsed, False
        except socket.timeout:
            continue
        except (ConnectionError, OSError) as e:
            total_elapsed = prior_elapsed + (time.time() - session_start)
            _RUN_STATE["afk_seconds"] = total_elapsed
            print(f"\n  [{time.strftime('%Y-%m-%d %H:%M:%S')}] [断线] {e}, 累计已挂机 {total_elapsed/60:.1f} 分钟")
            return None, seq, total_elapsed, False
        except KeyboardInterrupt:
            _RUN_STATE["status"] = "用户手动中断 (KeyboardInterrupt)"
            print(f"\n  用户中断")
            raise


# ===================== 奖励领取 =====================
def claim_rewards(game_sock: socket.socket, user_id: int, seq: int) -> bool:
    """领取 5 个阶段的在线奖励"""
    _RUN_STATE["status"] = "正在领取在线奖励"
    print(f"\n{'='*50}")
    print(f"  开始领取在线奖励")
    print(f"  当前 _seq = {seq}")
    print(f"{'='*50}\n")

    # Step 1: 打开活动面板 CMD=9245
    print(f"  [1] CMD=9245 打开活动面板 ...")
    pkt, seq = build_encrypted_packet(CMD_ACTIVITY_OPEN, user_id, CMD_9245_PLAIN, seq)
    game_sock.sendall(pkt)
    time.sleep(0.8)
    responses = read_all_available(game_sock, timeout=0.8)
    for r in responses:
        h = r['header']
        print(f"    <- CMD={h['cmd']} result={h['result']}")
    print(f"    完成, seq={seq}")

    # Step 2: 先领取前 4 个阶段 CMD=1243 (stage 61-64)
    for idx, stage_id in enumerate(REWARD_STAGE_IDS[:4]):
        desc = ["1分钟", "5分钟", "30分钟", "60分钟"][idx]
        print(f"  [2.{idx+1}] CMD=1243 领取 {desc} 奖励 (stage={stage_id}) ...")
        body_plain = struct.pack('>III', stage_id, 1, 1)
        pkt, seq = build_encrypted_packet(CMD_CLAIM_REWARD, user_id, body_plain, seq)
        game_sock.sendall(pkt)
        time.sleep(2.0)
        responses = read_all_available(game_sock, timeout=0.8)
        for r in responses:
            h = r['header']
            bp = r['body'].hex()[:30] if len(r['body']) > 0 else "(empty)"
            print(f"    <- CMD={h['cmd']} result={h['result']} body={bp}")
        print(f"    完成, seq={seq}")

    # Step 3: 确认前 4 个阶段 CMD=10101
    print(f"  [3] CMD=10101 确认前 4 个阶段 (value=31005) ...")
    body_plain = struct.pack('>I', 31005)
    pkt, seq = build_encrypted_packet(CMD_CLAIM_FINALIZE, user_id, body_plain, seq)
    game_sock.sendall(pkt)
    time.sleep(0.8)
    responses = read_all_available(game_sock, timeout=0.8)
    for r in responses:
        h = r['header']
        bp = r['body'].hex()[:30] if len(r['body']) > 0 else "(empty)"
        print(f"    <- CMD={h['cmd']} result={h['result']} body={bp}")
    print(f"    完成, seq={seq}")

    # Step 4: 单独领取第 5 个阶段 CMD=1243 (stage 65, 90分钟, 不需要 CMD=10101)
    print(f"  [4] CMD=1243 领取 90分钟 奖励 (stage=65) ...")
    body_plain = struct.pack('>III', 65, 1, 1)
    pkt, seq = build_encrypted_packet(CMD_CLAIM_REWARD, user_id, body_plain, seq)
    game_sock.sendall(pkt)
    time.sleep(2.0)
    responses = read_all_available(game_sock, timeout=0.8)
    for r in responses:
        h = r['header']
        bp = r['body'].hex()[:30] if len(r['body']) > 0 else "(empty)"
        print(f"    <- CMD={h['cmd']} result={h['result']} body={bp}")
    print(f"    完成, seq={seq}")

    print(f"\n  奖励领取完成! 最终 seq={seq}")
    return True


# ===================== 服务器连接方式 =====================
def try_encrypted_201(game_sock: socket.socket, user_id: int,
                       session: bytes, version_str: bytes, server_info: dict) -> bool:
    print(f"  [方式A] 加密 CMD=201 ...")
    pkt = build_encrypted_201_packet(user_id, session, version_str)
    game_sock.sendall(pkt)
    try:
        hd = recv_exact(game_sock, HEADER_LEN)
        h = parse_header(hd)
        body_len = h['total_len'] - HEADER_LEN
        if body_len > 0:
            recv_exact(game_sock, body_len)
        if h['result'] == 0:
            print(f"  [方式A] CMD=201 成功!")
            return True
        else:
            print(f"  [方式A] CMD=201 返回 result={h['result']}")
            return False
    except Exception as e:
        print(f"  [方式A] CMD=201 失败: {e}")
        return False


def try_raw_201(game_sock: socket.socket, user_id: int, session: bytes,
                version_str: bytes, server_id: int) -> bool:
    print(f"  [方式B] Raw CMD=201 (onlineSerLogin, serverID={server_id}) ...")
    pkt = build_raw_201_packet(user_id, session, server_id, version_str)
    game_sock.sendall(pkt)
    try:
        hd = recv_exact(game_sock, HEADER_LEN)
        h = parse_header(hd)
        body_len = h['total_len'] - HEADER_LEN
        body = recv_exact(game_sock, body_len) if body_len > 0 else b''
        if h['result'] == 0:
            print(f"  [方式B] CMD=201 成功! body_len={len(body)}")
            return True
        else:
            print(f"  [方式B] CMD=201 返回 result={h['result']}")
            return False
    except Exception as e:
        print(f"  [方式B] CMD=201 失败: {e}")
        return False


def send_enter_game(game_sock: socket.socket, user_id: int,
                     sit_id: int = 1, map_id: int = 0) -> dict:
    print(f"  [CMD=403] enterGame(sitID={sit_id}, mapID={map_id}) ...")
    body = struct.pack('>I', sit_id) + struct.pack('>I', map_id)
    pkt = build_packet(CMD_ENTER_GAME_ROOM, user_id, body, version=1)
    game_sock.sendall(pkt)
    hd = recv_exact(game_sock, HEADER_LEN)
    h = parse_header(hd)
    body_len = h['total_len'] - HEADER_LEN
    body = recv_exact(game_sock, body_len) if body_len > 0 else b''
    print(f"  [CMD=403] result={h['result']} body_len={len(body)}")
    return {'header': h, 'body': body}


def send_connect_game_server(game_sock: socket.socket, user_id: int,
                              site_id: int = 1) -> dict:
    print(f"  [CMD=10016] CONNECT_GAME_SERVER_REQ(siteID={site_id}) ...")
    pkt = build_packet(CMD_CONNECT_GAME_SERVER, user_id,
                        struct.pack('>I', site_id), version=1)
    game_sock.sendall(pkt)
    hd = recv_exact(game_sock, HEADER_LEN)
    h = parse_header(hd)
    body_len = h['total_len'] - HEADER_LEN
    body = recv_exact(game_sock, body_len) if body_len > 0 else b''
    print(f"  [CMD=10016] result={h['result']} body_len={len(body)}")
    return {'header': h, 'body': body}


def send_check_game_ser(game_sock: socket.socket, user_id: int,
                         session112: bytes) -> int:
    print(f"  [CMD=30001] CHECK_GAME_SER (session={len(session112)} bytes) ...")
    pkt, seq = build_encrypted_packet(CMD_CHECK_GAME_SER, user_id, session112, 128)
    game_sock.sendall(pkt)
    time.sleep(0.2)
    responses = read_all_available(game_sock, timeout=0.6)
    for r in responses:
        h = r['header']
        print(f"  [CMD=30001] <- CMD={h['cmd']} result={h['result']} body_len={len(r['body'])}")
    return seq


def login_and_enter_game() -> tuple:
    """
    执行完整登录与进入游戏服流程
    返回 (game_sock, user_id, final_seq)
    """
    # ---- [1] 短路连接登录服务器 (仅握手一次) ----
    sock, _, _ = connect_login_server()

    # ---- [2] 密码加密 ----
    encrypted_pwd = hashlib.md5(
        hashlib.md5(PASSWORD.encode('utf-8')).hexdigest().encode('utf-8')
    ).hexdigest()

    # ---- [3] CMD=101 验证码 ----
    sock.sendall(build_packet(101, 0, b''))
    hd = recv_exact(sock, HEADER_LEN)
    h = parse_header(hd)
    body_len = h['total_len'] - HEADER_LEN
    auth_code = recv_exact(sock, body_len) if body_len > 0 else b''
    if h['result'] != 0:
        sock.close()
        raise RuntimeError(f"验证码获取失败 result={h['result']}")
    print(f"[3] 验证码获取成功 (CMD=101) auth_len={len(auth_code)}")

    # ---- [4] CMD=103 登录 ----
    time.sleep(0.1)
    auth_field = auth_code + b'\x00' * 18 if len(auth_code) == 4 else b'\x00' * 22
    body = encrypted_pwd.encode('utf-8')
    body += struct.pack('>I', 0) + struct.pack('>I', 1) + struct.pack('>I', 0)
    body += auth_field + b'0' + b'\x00' * 63

    sock.sendall(build_packet(CMD_LOGIN, USER_ID, body))
    hd = recv_exact(sock, HEADER_LEN)
    h = parse_header(hd)
    body_len = h['total_len'] - HEADER_LEN
    body_data = recv_exact(sock, body_len) if body_len > 0 else b''
    if h['result'] != 0:
        sock.close()
        raise RuntimeError(f"登录失败 result={h['result']}")

    session = body_data[4:20]
    user_id = h['user_id']
    print(f"[4] 登录成功! userID={user_id}, session={session.hex()[:16]}...")

    # ---- [5] CMD=105 获取服务器列表 ----
    sock.sendall(build_packet(CMD_GET_GOOD_SERVER_LIST, user_id,
                              session + struct.pack('>I', 0)))
    hd = recv_exact(sock, HEADER_LEN)
    h = parse_header(hd)
    body_len = h['total_len'] - HEADER_LEN
    body_data = recv_exact(sock, body_len) if body_len > 0 else b''
    sock.close()

    count = struct.unpack('>I', body_data[:4])[0] if len(body_data) >= 4 else 0
    server_list = []
    offset = 4
    for _ in range(count):
        if offset + 30 > len(body_data):
            break
        sid = struct.unpack('>I', body_data[offset:offset+4])[0]
        user_cnt = struct.unpack('>I', body_data[offset+4:offset+8])[0]
        ip = body_data[offset+8:offset+24].decode('utf-8', errors='ignore').strip('\x00')
        port = struct.unpack('>H', body_data[offset+24:offset+26])[0]
        server_list.append({'id': sid, 'ip': ip, 'port': port, 'users': user_cnt})
        offset += 30

    print(f"[5] 服务器列表: {len(server_list)} 个")
    for i, s in enumerate(server_list[:5]):
        print(f"    [{i}] id={s['id']} {s['ip']}:{s['port']} (online={s['users']})")

    # ---- [6] 预计算 versionStr ----
    version_str = calc_version_string(session)

    # ---- [7] 构建候选游戏服务器 (按 (ip, port) 去重, 优先使用服务器返回的真实端口) ----
    KNOWN_PORTS = [1201, 1865, 1863]
    candidates = []
    seen_endpoints = set()

    for s in server_list:
        ep = (s['ip'], s['port'])
        if ep not in seen_endpoints:
            seen_endpoints.add(ep)
            candidates.append((s['ip'], s['port'], s['id']))

    for s in server_list:
        for port in KNOWN_PORTS:
            ep = (s['ip'], port)
            if ep not in seen_endpoints:
                seen_endpoints.add(ep)
                candidates.append((s['ip'], port, s['id']))

    print(f"\n[7] 候选连接目标 ({len(candidates)} 个):")
    for ip, port, sid in candidates:
        print(f"    {ip}:{port} (serverID={sid})")

    for ip, port, server_id in candidates:
        print(f"\n{'='*50}")
        print(f"  尝试 {ip}:{port} (serverID={server_id})")

        try:
            game_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            game_sock.settimeout(5)
            game_sock.connect((ip, port))
            enable_keepalive(game_sock)
            print(f"  连接成功")
        except Exception as e:
            print(f"  连接失败: {e}")
            continue

        if try_encrypted_201(game_sock, user_id, session, version_str, {'id': server_id}):
            try:
                seq = run_init_sequence(game_sock, user_id, initial_seq=65)
                return game_sock, user_id, seq
            except Exception as e:
                print(f"  初始化失败: {e}")
                game_sock.close()
                continue

        game_sock.close()
        try:
            game_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            game_sock.settimeout(5)
            game_sock.connect((ip, port))
            enable_keepalive(game_sock)
        except Exception as e:
            print(f"  重连失败: {e}")
            continue

        if not try_raw_201(game_sock, user_id, session, version_str, server_id):
            game_sock.close()
            continue

        try:
            eg_resp = send_enter_game(game_sock, user_id)
            if eg_resp['header']['result'] != 0:
                game_sock.close()
                continue

            gs_resp = send_connect_game_server(game_sock, user_id)
            if gs_resp['header']['result'] != 0:
                game_sock.close()
                continue

            body = gs_resp['body']
            if len(body) < 18:
                game_sock.close()
                continue

            game_ip = body[:16].decode('utf-8', errors='ignore').strip('\x00')
            game_port = struct.unpack('>H', body[16:18])[0]
            session112 = body[18:130] if len(body) >= 130 else body[18:]
            game_sock.close()

            gs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            gs.settimeout(5)
            gs.connect((game_ip, game_port))
            enable_keepalive(gs)

            seq = send_check_game_ser(gs, user_id, session112)
            seq = run_init_sequence(gs, user_id, initial_seq=seq)
            return gs, user_id, seq
        except Exception as e:
            print(f"  重定向流程失败: {e}")
            try:
                game_sock.close()
            except Exception:
                pass
            continue

    raise RuntimeError("所有候选游戏服务器连接均失败!")


# ===================== 主流程 =====================
def main() -> bool:
    parser = argparse.ArgumentParser(description="摩尔庄园自动挂机与领奖脚本")
    parser.add_argument("-d", "--duration", type=float, default=AFK_DURATION / 60.0,
                        help="挂机时长(分钟), 默认 95 分钟")
    parser.add_argument("--claim-only", action="store_true",
                        help="登录后跳过挂机直接领取奖励")
    args, _ = parser.parse_known_args()

    target_duration = 0.0 if args.claim_only else max(0.0, args.duration * 60.0)

    print(f"\n{'#'*50}")
    print(f"  摩尔庄园挂机脚本 AFK_25 启动 [{_RUN_STATE['start_str']}]")
    print(f"  用户ID: {USER_ID}")
    dur_str = ("0 分钟 (直接领奖)" if args.claim_only
               else f"{int(target_duration) // 3600} 小时 {(int(target_duration) % 3600) // 60} 分钟"
               if target_duration >= 3600
               else f"{int(target_duration) // 60} 分钟")
    print(f"  挂机时长: {dur_str}")
    print(f"  日志文件: {LOG_FILE}")
    print(f"{'#'*50}\n")

    if not PASSWORD:
        _RUN_STATE["status"] = "未配置登录密码 (请设置环境变量 MOLE_PASSWORD 或在同目录创建 mole_secret.txt)"
        print(f"[!] 错误: {_RUN_STATE['status']}")
        return False

    total_elapsed = 0.0
    reconnect_count = 0
    final_sock = None
    final_seq = 0
    user_id = USER_ID

    while True:
        try:
            _RUN_STATE["status"] = "正在登录与连接游戏服"
            final_sock, user_id, final_seq = login_and_enter_game()
        except Exception as e:
            reconnect_count += 1
            _RUN_STATE["status"] = f"登录/进服失败 ({e})"
            print(f"\n[!] 登录/进服失败 ({reconnect_count}/{MAX_RECONNECT_ATTEMPTS}): {e}")
            if reconnect_count >= MAX_RECONNECT_ATTEMPTS:
                _RUN_STATE["status"] = f"登录/进服连续失败达上限 ({MAX_RECONNECT_ATTEMPTS}次): {e}"
                print("\n[!] 已达到最大重试次数, 退出脚本。")
                return False
            wait_sec = min(30, 5 * reconnect_count)
            print(f"    等待 {wait_sec} 秒后重试...\n")
            time.sleep(wait_sec)
            continue

        if target_duration <= 0:
            break

        try:
            final_sock, final_seq, total_elapsed, completed = afk_loop(
                final_sock, user_id, final_seq,
                target_duration=target_duration,
                prior_elapsed=total_elapsed
            )
        except KeyboardInterrupt:
            _RUN_STATE["status"] = "用户手动中断 (KeyboardInterrupt)"
            if final_sock:
                try:
                    final_sock.close()
                except Exception:
                    pass
            return False

        if completed and final_sock is not None:
            break

        # 挂机中途断线 -> 自动重连并继续剩余时长
        reconnect_count += 1
        _RUN_STATE["status"] = f"挂机中途断线重连中 (第 {reconnect_count} 次)"
        if reconnect_count > MAX_RECONNECT_ATTEMPTS:
            _RUN_STATE["status"] = f"断线重连超过上限 ({MAX_RECONNECT_ATTEMPTS} 次) 中断退出"
            print(f"\n  [!] 断线重连次数超过上限 ({MAX_RECONNECT_ATTEMPTS} 次), 无法完成挂机")
            return False
        wait_sec = min(30, 5 * reconnect_count)
        print(f"\n  [重连] 将在 {wait_sec} 秒后进行第 {reconnect_count}/{MAX_RECONNECT_ATTEMPTS} 次重连...")
        time.sleep(wait_sec)

    # ---- [9] 领取在线奖励 ----
    success = False
    try:
        claim_rewards(final_sock, user_id, final_seq)
        _RUN_STATE["status"] = "正常完成 (已挂满时长并执行领奖)"
        success = True
    except Exception as e:
        print(f"\n  领取奖励时连接异常 ({e}), 尝试重新登录补领...")
        try:
            if final_sock:
                final_sock.close()
            final_sock, user_id, final_seq = login_and_enter_game()
            claim_rewards(final_sock, user_id, final_seq)
            _RUN_STATE["status"] = "正常完成 (重连后补领奖励成功)"
            success = True
        except Exception as e2:
            _RUN_STATE["status"] = f"挂机完成但补领奖励失败 ({e2})"
            print(f"\n  补领奖励失败: {e2}")
            traceback.print_exc()
    finally:
        try:
            if final_sock:
                final_sock.close()
        except Exception:
            pass
        _write_exit_summary()
    return success


if __name__ == "__main__":
    ok = False
    try:
        ok = bool(main())
    except KeyboardInterrupt:
        _RUN_STATE["status"] = "用户手动中断 (KeyboardInterrupt)"
    except Exception as e:
        _RUN_STATE["status"] = f"未捕获异常崩溃 ({type(e).__name__}: {e})"
        print(f"\n  发生异常: {e}")
        traceback.print_exc()
    finally:
        _write_exit_summary()

    # 仅在交互式终端 (如 Windows 双击黑框) 下等待回车, Linux 后台/定时任务直接退出
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            print(f"按 Enter 键退出...")
            input()
        except (EOFError, OSError, KeyboardInterrupt):
            pass
    elif not ok:
        sys.exit(1)
