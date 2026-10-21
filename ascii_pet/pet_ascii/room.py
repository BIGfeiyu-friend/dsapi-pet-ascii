"""虚拟房间（v1.0）：属于她自己的一个小文件夹。只有桌宠进程里的她会改动这里（原始日志、状态、房间记忆也只由桌宠进程写），
主人的“房间”窗口是另一个进程，只读这些文件，送礼物时写进 inbox.jsonl 和「礼物」文件夹。

目录（字符画数据目录下的 room/）：
  space/          她的房间（她能看到、能改动的全部范围）；space/礼物/ 是主人送东西进来的地方
  trash/          回收站：她删除的东西、被覆盖 / 修改前的旧版本都先放这里（不会直接消失）
  snapshots/      每天一份房间快照（zip）
  room_log.jsonl  原始日志：每次醒来、每个想法、每个动作、每件礼物（只追加，不修改）
  room_state.json 精力 / 无聊 / 想念、日程、用量（room_life）
  room_memory.json 房间记忆：短期 → 印象（几天）→ 长期（room_memory）
  inbox.jsonl     主人窗口 → 桌宠进程：送来的礼物
这里和桌宠本体的记忆完全分开，也不会影响副人格。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import date
from pathlib import Path

from .config import data_dir

GIFT_DIR = "礼物"
WRITE_EXT = (".txt", ".md")                 # 她能创建、编辑的文件
READ_EXT = WRITE_EXT + (".csv", ".json", ".log", ".py", ".js", ".html", ".htm", ".xml", ".yaml", ".yml",
                        ".ini", ".srt", ".lrc", ".tsv", ".rst")      # 礼物里她能读懂的文字文件
MAX_FILE_CHARS = 20000          # 她写的单个文件最多多少字
MAX_WRITE_CHARS = 4000          # 一次动作最多写多少字
MAX_READ_CHARS = 3000           # 一次读回多少字（更长的会截断，告诉她还有多少）
MAX_GIFT_BYTES = 20 * 1024 * 1024
MAX_READ_BYTES = 400 * 1024     # 超过这么大的文字礼物只读开头
MAX_FILES = 300
MAX_DEPTH = 4
MAX_NAME = 40
TRASH_KEEP = 300
_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class RoomError(ValueError):
    """动作不合规（路径越界、类型不允许、超出上限…）。信息是给她看的，说清楚为什么。"""


def room_root() -> Path:
    return data_dir() / "room"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:      # 换行保持 LF（不在 Windows 上变成 CRLF）
        f.write(text)
    os.replace(tmp, path)


def _stamp(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts or time.time()))


class Room:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or room_root())
        self.space = self.root / "space"
        self.trash = self.root / "trash"
        self.snaps = self.root / "snapshots"
        self.log_path = self.root / "room_log.jsonl"
        self.inbox_path = self.root / "inbox.jsonl"
        self._lock = threading.RLock()
        self._next_id = None
        for d in (self.space, self.space / GIFT_DIR, self.trash, self.snaps):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 路径
    def resolve(self, rel: str, must_exist: bool = True, kind: str = "") -> Path:
        """把她写的相对路径变成房间里的真实路径；任何越界、隐藏、保留名、过深都拒绝。kind：file / dir / ""。"""
        raw = str(rel or "").strip().replace("\\", "/")
        if raw.startswith(("space/", "./")):
            raw = raw.split("/", 1)[1]
        raw = raw.strip("/")
        if not raw:
            raise RoomError("路径是空的")
        if re.match(r"^[a-zA-Z]:", raw) or raw.startswith("~"):
            raise RoomError("只能用房间里的相对路径（例如 日记.md 或 画/猫.txt）")
        parts = [p.strip() for p in raw.split("/")]
        if len(parts) > MAX_DEPTH:
            raise RoomError(f"文件夹最多 {MAX_DEPTH} 层")
        for p in parts:
            if p in ("", ".", ".."):
                raise RoomError("路径里不能有 .. 或空的部分（不能走出房间）")
            if p.startswith("."):
                raise RoomError("名字不能以 . 开头")
            if len(p) > MAX_NAME:
                raise RoomError(f"名字最多 {MAX_NAME} 个字")
            if _BAD_CHARS.search(p) or p.endswith((" ", ".")):
                raise RoomError('名字里不能有 < > : " | ? * \\ 这些字符，也不能以空格或点结尾')
            if p.split(".")[0].lower() in _RESERVED:
                raise RoomError(f"「{p}」是系统保留的名字")
        path = self.space.joinpath(*parts)
        try:
            real = path.resolve()
            real.relative_to(self.space.resolve())
        except (OSError, ValueError):
            raise RoomError("这个路径在房间外面")
        if must_exist and not path.exists():
            raise RoomError(f"房间里没有「{'/'.join(parts)}」")
        if path.exists() and kind == "file" and not path.is_file():
            raise RoomError(f"「{'/'.join(parts)}」是文件夹，不是文件")
        if path.exists() and kind == "dir" and not path.is_dir():
            raise RoomError(f"「{'/'.join(parts)}」是文件，不是文件夹")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.space).as_posix()

    def is_gift(self, path: Path) -> bool:
        try:
            return self.rel(path).split("/")[0] == GIFT_DIR
        except ValueError:
            return False

    # ------------------------------------------------------------------ 查看
    def walk(self) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.space):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            base = Path(dirpath)
            for d in dirnames:
                out.append(base / d)
            for f in sorted(filenames):
                if not f.startswith(".") and not f.endswith(".tmp"):
                    out.append(base / f)
        return sorted(out, key=lambda p: self.rel(p))

    def tree(self) -> list[dict]:
        items = []
        for p in self.walk():
            try:
                st = p.stat()
            except OSError:
                continue
            rel = self.rel(p)
            it = {"path": rel, "type": "dir" if p.is_dir() else "file", "mtime": st.st_mtime,
                  "depth": rel.count("/"), "gift": rel.split("/")[0] == GIFT_DIR}
            if it["type"] == "file":
                it["size"] = st.st_size
                it["text"] = p.suffix.lower() in READ_EXT
            items.append(it)
        return items

    def signature(self) -> dict:
        """路径 → (大小, 修改时间)：用来发现“不是她做的”变化（主人在资源管理器里动过、送了礼物）。"""
        return {it["path"]: [it.get("size", -1), round(it["mtime"], 1)] for it in self.tree()}

    def tree_text(self, items: list | None = None, max_items: int = 80, mark: dict | None = None,
                  seen: dict | None = None) -> str:
        items = self.tree() if items is None else items
        if not items:
            return "（空的）"
        lines = []
        for it in items[:max_items]:
            name = it["path"].rsplit("/", 1)[-1]
            pad = "  " * it["depth"]
            if it["type"] == "dir":
                empty = not any(o["path"].startswith(it["path"] + "/") for o in items)
                lines.append(f"{pad}{name}/" + ("（空的）" if empty else ""))
                continue
            if it.get("text"):
                try:
                    info = f"{len(_read_text(self.space / it['path']))} 字"
                except OSError:
                    info = _size_text(it.get("size", 0))
            else:
                info = _image_info(self.space / it["path"], it, (seen or {}).get(it["path"]))
            note = (mark or {}).get(it["path"], "")
            lines.append(f"{pad}{name}（{info}）{note}")
        if len(items) > max_items:
            lines.append(f"……还有 {len(items) - max_items} 项")
        return "\n".join(lines)

    def count_files(self) -> int:
        return sum(1 for p in self.walk() if p.is_file())

    def read(self, rel: str, max_chars: int = MAX_READ_CHARS, offset: int = 0) -> dict:
        p = self.resolve(rel, kind="file")
        if p.suffix.lower() not in READ_EXT:
            raise RoomError(f"「{self.rel(p)}」不是文字文件，你看不懂里面的内容（只能看到名字和大小）")
        text = _read_text(p, MAX_READ_BYTES)
        offset = max(0, int(offset or 0))
        part = text[offset:offset + max_chars]
        return {"path": self.rel(p), "text": part, "chars": len(text), "offset": offset,
                "more": max(0, len(text) - offset - len(part))}

    # ------------------------------------------------------------------ 改动（她的动作）
    def _check_writable(self, p: Path) -> None:
        if p.suffix.lower() not in WRITE_EXT:
            raise RoomError(f"只能创建 / 编辑 {' '.join(WRITE_EXT)} 文件")

    def _limit(self, text: str) -> str:
        text = str(text or "")
        if len(text) > MAX_WRITE_CHARS:
            raise RoomError(f"一次最多写 {MAX_WRITE_CHARS} 字（这次 {len(text)} 字），可以分几次写")
        return text

    def write(self, rel: str, content: str, overwrite: bool = False) -> dict:
        rel = _with_ext(rel)
        p = self.resolve(rel, must_exist=False, kind="file")
        self._check_writable(p)
        content = self._limit(content)
        if len(content) > MAX_FILE_CHARS:
            raise RoomError(f"单个文件最多 {MAX_FILE_CHARS} 字")
        existed = p.exists()
        if existed and not overwrite:
            raise RoomError(f"「{self.rel(p)}」已经存在：要覆盖就写 overwrite:true，要接着写用 append")
        if not existed and self.count_files() >= MAX_FILES:
            raise RoomError(f"房间里最多 {MAX_FILES} 个文件，先整理一下吧")
        p.parent.mkdir(parents=True, exist_ok=True)
        old = _read_text(p) if existed else ""
        if existed:
            self._to_trash(p, copy=True, why="overwrite")
        _atomic_write(p, content)
        return {"path": self.rel(p), "chars": len(content), "created": not existed, "old_chars": len(old)}

    def append(self, rel: str, content: str) -> dict:
        rel = _with_ext(rel)
        p = self.resolve(rel, must_exist=False, kind="file")
        self._check_writable(p)
        content = self._limit(content)
        if not p.exists():
            return self.write(rel, content)
        old = _read_text(p)
        if len(old) + len(content) > MAX_FILE_CHARS:
            raise RoomError(f"「{self.rel(p)}」已经 {len(old)} 字，再写就超过 {MAX_FILE_CHARS} 字了，开一个新文件吧")
        sep = "" if not old or old.endswith("\n") or content.startswith("\n") else "\n"
        _atomic_write(p, old + sep + content)
        return {"path": self.rel(p), "chars": len(old) + len(sep) + len(content), "added": len(content)}

    def edit(self, rel: str, old: str, new: str) -> dict:
        p = self.resolve(rel, kind="file")
        self._check_writable(p)
        old, new = str(old or ""), self._limit(new)
        text = _read_text(p)
        if not old:
            raise RoomError("edit 需要写出要替换的原文 old")
        n = text.count(old)
        if n == 0:
            raise RoomError(f"「{self.rel(p)}」里找不到要替换的原文（要一字不差）；可以先 read 再改，或者整个覆盖")
        out = text.replace(old, new, 1)
        if len(out) > MAX_FILE_CHARS:
            raise RoomError(f"改完会超过 {MAX_FILE_CHARS} 字")
        self._to_trash(p, copy=True, why="edit")
        _atomic_write(p, out)
        return {"path": self.rel(p), "chars": len(out), "replaced": len(old), "with": len(new), "matches": n}

    def mkdir(self, rel: str) -> dict:
        p = self.resolve(rel, must_exist=False)
        if p.exists():
            raise RoomError(f"「{self.rel(p)}」已经有了")
        p.mkdir(parents=True)
        return {"path": self.rel(p)}

    def rename(self, rel: str, new_name: str) -> dict:
        p = self.resolve(rel)
        self._guard_gift_root(p)
        new_name = str(new_name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if p.is_file() and "." not in new_name:
            new_name += p.suffix
        if p.is_file() and Path(new_name).suffix.lower() != p.suffix.lower() and \
                Path(new_name).suffix.lower() not in WRITE_EXT:
            raise RoomError("改名不能把文件改成别的类型")
        parent = "" if p.parent == self.space else self.rel(p.parent) + "/"
        target = self.resolve(parent + new_name, must_exist=False)
        if target.exists():
            raise RoomError(f"「{self.rel(target)}」已经有了")
        old = self.rel(p)
        p.rename(target)
        return {"path": old, "to": self.rel(target)}

    def move(self, rel: str, to_dir: str) -> dict:
        p = self.resolve(rel)
        self._guard_gift_root(p)
        to_dir = str(to_dir or "").strip().strip("/")
        dest_dir = self.space if to_dir in ("", ".", "/") else self.resolve(to_dir, must_exist=False)
        if dest_dir.exists() and not dest_dir.is_dir():
            raise RoomError(f"「{to_dir}」是文件，不是文件夹")
        if p.is_dir() and (dest_dir == p or p in dest_dir.parents):
            raise RoomError("不能把文件夹搬进它自己里面")
        target = dest_dir / p.name
        self.resolve(self.rel(target), must_exist=False)        # 深度、名字检查
        if target.exists():
            raise RoomError(f"「{self.rel(target)}」已经有了")
        dest_dir.mkdir(parents=True, exist_ok=True)
        old = self.rel(p)
        shutil.move(str(p), str(target))
        return {"path": old, "to": self.rel(target)}

    def delete(self, rel: str) -> dict:
        p = self.resolve(rel)
        self._guard_gift_root(p)
        old = self.rel(p)
        where = self._to_trash(p, copy=False, why="delete")
        return {"path": old, "trash": where}

    def _guard_gift_root(self, p: Path) -> None:
        if p == self.space / GIFT_DIR:
            raise RoomError(f"「{GIFT_DIR}」文件夹是主人送东西进来的地方，不能删除、改名或搬走（里面的东西可以）")

    def _to_trash(self, p: Path, copy: bool, why: str) -> str:
        name = f"{_stamp()}_{why}_{self.rel(p).replace('/', '__')}"
        dest = self.trash / name
        i = 1
        while dest.exists():
            dest = self.trash / f"{name}.{i}"
            i += 1
        if copy:
            shutil.copy2(p, dest)
        else:
            shutil.move(str(p), str(dest))
        self._prune_trash()
        return dest.name

    def _prune_trash(self) -> None:
        items = sorted(self.trash.iterdir(), key=lambda q: q.name)
        for q in items[:max(0, len(items) - TRASH_KEEP)]:
            try:
                shutil.rmtree(q) if q.is_dir() else q.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 原始日志（只追加）
    def append_log(self, event: dict) -> dict:
        with self._lock:
            if self._next_id is None:
                self._next_id = self._last_log_id() + 1
            ev = {"id": f"r{self._next_id}", "ts": round(time.time(), 2), **event}
            self._next_id += 1
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
            return ev

    def _last_log_id(self) -> int:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return 0
        for line in reversed(tail):
            try:
                return int(json.loads(line)["id"][1:])
            except (ValueError, KeyError, TypeError):
                continue
        return 0

    def read_log(self, offset: int = 0, limit: int = 2000) -> tuple[list, int]:
        """从字节偏移 offset 读新日志行（主人窗口轮询用）。返回 (事件, 新偏移)。"""
        return _read_jsonl(self.log_path, offset, limit)

    def tail_log(self, n: int = 200) -> list:
        """最后 n 条日志（从文件末尾往前一块一块读；带思考过程的行可能有十几 KB）。"""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                buf = b""
                while pos > 0 and buf.count(b"\n") <= n:
                    step = min(256 * 1024, pos)
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step) + buf
        except OSError:
            return []
        lines = buf.split(b"\n")
        if pos > 0:
            lines = lines[1:]                       # 第一行可能不完整
        out = []
        for line in lines[-(n + 1):]:
            if line.strip():
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    continue
        return out[-n:]

    # ------------------------------------------------------------------ 礼物（主人窗口写，桌宠进程读）
    def add_gift(self, src: str | Path, note: str = "") -> dict:
        src = Path(src)
        if not src.is_file():
            raise RoomError("只能送文件（文件夹请先打包或挑几个文件）")
        if src.stat().st_size > MAX_GIFT_BYTES:
            raise RoomError(f"文件太大了（上限 {MAX_GIFT_BYTES // 1024 // 1024} MB）")
        name = _safe_name(src.name)
        dest = _unique(self.space / GIFT_DIR / name)
        shutil.copy2(src, dest)
        return self._post_inbox({"type": "gift", "path": self.rel(dest), "name": src.name,
                                 "size": dest.stat().st_size, "note": str(note or "")[:200]})

    def add_gift_text(self, text: str, title: str = "") -> dict:
        text = str(text or "").strip()
        if not text:
            raise RoomError("便条是空的")
        name = _safe_name((title.strip() or "主人的便条") + time.strftime("_%m%d-%H%M") + ".txt")
        dest = _unique(self.space / GIFT_DIR / name)
        _atomic_write(dest, text[:MAX_FILE_CHARS])
        return self._post_inbox({"type": "gift", "path": self.rel(dest), "name": dest.name,
                                 "size": dest.stat().st_size, "note": "主人写的便条"})

    def _post_inbox(self, ev: dict) -> dict:
        ev = {"ts": round(time.time(), 2), **ev}
        with open(self.inbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
        return ev

    def read_inbox(self, offset: int = 0) -> tuple[list, int]:
        return _read_jsonl(self.inbox_path, offset, 500)

    # ------------------------------------------------------------------ 快照
    def snapshot_daily(self, keep: int = 14, max_bytes: int = 60 * 1024 * 1024) -> str:
        """每天一份 zip 快照（今天已有就跳过）。返回新快照的文件名，没做就返回空字符串。"""
        name = f"{date.today().isoformat()}.zip"
        dest = self.snaps / name
        if dest.exists():
            return ""
        files = [p for p in self.walk() if p.is_file()]
        if sum(p.stat().st_size for p in files) > max_bytes:
            return ""
        tmp = dest.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, self.rel(p))
            z.writestr(".tree.json", json.dumps(self.signature(), ensure_ascii=False))
        os.replace(tmp, dest)
        snaps = sorted(self.snaps.glob("*.zip"))
        for old in snaps[:max(0, len(snaps) - keep)]:
            try:
                old.unlink()
            except OSError:
                pass
        return name


# ---------------------------------------------------------------------- 小工具
def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _image_info(p: Path, it: dict, saw: str | None) -> str:
    """图片可以用 look 看（v1.2）；其它看不懂的格式只能看到名字和大小。"""
    try:
        from .room_image import image_size, is_image

        if is_image(p):
            w, h = image_size(p)
            info = f"图片 {w}×{h}，可以 look 看" if w else "图片，可以 look 看"
            return info + (f"；你看过，觉得是：{saw}" if saw else "")
    except Exception:
        pass
    return _size_text(it.get("size", 0)) + "，看不懂的格式（只能看到名字）"


def _read_text(p: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    with open(p, "rb") as f:
        raw = f.read(max_bytes)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").replace("\r\n", "\n")


def _with_ext(rel: str) -> str:
    rel = str(rel or "").strip()
    tail = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return rel if "." in tail.lstrip(".") else rel + ".txt"


def _safe_name(name: str) -> str:
    name = _BAD_CHARS.sub("_", str(name or "").strip()).lstrip(".").rstrip(". ") or "礼物"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    ext = ("." + ext[:10]) if ext else ""
    return stem[:MAX_NAME - len(ext) - 4] + ext


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    for i in range(2, 1000):
        q = p.with_name(f"{stem[:MAX_NAME - len(ext) - 4]}_{i}{ext}")
        if not q.exists():
            return q
    return p.with_name(f"{stem[:20]}_{_stamp()}{ext}")


def _read_jsonl(path: Path, offset: int, limit: int, skip_partial: bool = False) -> tuple[list, int]:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[:end + 1]
    lines = chunk.split(b"\n")
    if skip_partial and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            continue
        if len(out) >= limit:
            break
    return out, offset + len(chunk)


def similarity(a: str, b: str) -> float:
    """两段文字的相似度（字二元组的重合度，0~1）：判断“又做了差不多的事”、找相似的过去。不需要任何模型。"""
    sa, sb = _grams(a), _grams(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / ((len(sa) * len(sb)) ** 0.5)


def _grams(text: str) -> set:
    s = re.sub(r"\s+", " ", str(text or "").lower())
    s = re.sub(r"[，。！？、；：“”‘’（）()《》〈〉【】\[\],.!?;:\"'~…—\-]", " ", s)
    out = set()
    for tok in re.findall(r"[a-z0-9_]+|[^\sa-z0-9_]+", s):
        if re.match(r"[a-z0-9_]", tok):
            out.add(tok)
        elif len(tok) == 1:
            out.add(tok)
        else:
            out.update(tok[i:i + 2] for i in range(len(tok) - 1))
    return out
