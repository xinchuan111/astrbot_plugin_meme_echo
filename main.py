from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
import astrbot.api.message_components as Comp


def md5_bytes_upper(b: bytes) -> str:
    return hashlib.md5(b).hexdigest().upper()


@register("meme_echo", "YourName", "群聊表情包命中即复读（命令收录+别名管理）", "1.1.0")
class MemeEcho(Star):
    """
    /meme add               收录一张表情包（先发命令再发图，或命令同条带图）
    /meme name <KEY> <别名> 绑定别名
    /meme show <KEY|别名>   查看详情
    /meme list              列表（含别名）
    /meme del <KEY|别名>    删除
    /meme reload            重建索引
    """

    async def initialize(self):
        # ✅ 所有初始化都放这里，不要写 __init__
        self.data_dir = Path(StarTools.get_data_dir(self.plugin_name))
        self.meme_dir = self.data_dir / "memes"
        self.meme_dir.mkdir(parents=True, exist_ok=True)

        self.index_path = self.data_dir / "index.json"   # key -> filename
        self.alias_path = self.data_dir / "alias.json"   # alias -> key

        self.index: Dict[str, str] = {}
        self.alias: Dict[str, str] = {}
        self.awaiting: Dict[Tuple[str, str], float] = {}  # (group_id, user_id) -> expire_ts

        self._load_or_rebuild()
        logger.error(f"✅ meme_echo initialized. count={len(self.index)} alias={len(self.alias)} dir={self.meme_dir}")

    # ---------- state ----------
    def _load_or_rebuild(self) -> None:
        self._load_index()
        if not self.index:
            self._rebuild_index()
        self._load_alias()

    def _load_index(self) -> None:
        try:
            if self.index_path.exists():
                data = json.loads(self.index_path.read_text("utf-8"))
                self.index = {str(k).upper(): str(v) for k, v in data.items()}
        except Exception:
            self.index = {}

    def _save_index(self) -> None:
        self.index_path.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), "utf-8")

    def _rebuild_index(self) -> None:
        self.index.clear()
        for p in self.meme_dir.glob("*"):
            if not p.is_file():
                continue
            stem = p.stem.upper()
            if len(stem) == 32:
                self.index[stem] = p.name
        self._save_index()

    def _load_alias(self) -> None:
        try:
            if self.alias_path.exists():
                data = json.loads(self.alias_path.read_text("utf-8"))
                self.alias = {str(a).strip(): str(k).upper() for a, k in data.items()}
        except Exception:
            self.alias = {}

    def _save_alias(self) -> None:
        self.alias_path.write_text(json.dumps(self.alias, ensure_ascii=False, indent=2), "utf-8")

    # ---------- helpers ----------
    def _extract_first_image(self, event: AstrMessageEvent) -> Optional[Comp.Image]:
        msg = event.message_obj
        if not msg or not msg.message:
            return None
        for seg in msg.message:
            if isinstance(seg, Comp.Image):
                return seg
        return None

    def _get_group_user_key(self, event: AstrMessageEvent) -> Tuple[str, str]:
        msg = event.message_obj
        group_id = str(getattr(msg, "group_id", "") or getattr(event, "group_id", "") or "")
        user_id = str(getattr(msg, "user_id", "") or getattr(event, "user_id", "") or getattr(msg, "sender_id", "") or "")
        return (group_id, user_id)

    def _resolve_key(self, key_or_alias: str) -> Optional[str]:
        s = (key_or_alias or "").strip()
        if len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s):
            return s.upper()
        return self.alias.get(s)

    def _reverse_alias(self, key: str) -> Optional[str]:
        key = key.upper()
        for a, k in self.alias.items():
            if k == key:
                return a
        return None

    def _save_bytes_as_meme(self, data: bytes, ext: str) -> str:
        key = md5_bytes_upper(data)
        ext = (ext or ".png").lower()
        if not ext.startswith("."):
            ext = "." + ext
        filename = f"{key}{ext}"
        dst = self.meme_dir / filename
        if not dst.exists():
            dst.write_bytes(data)
        self.index[key] = filename
        self._save_index()
        return key

    def _delete_key(self, key: str) -> bool:
        key = key.upper()
        name = self.index.get(key)
        if not name:
            return False

        p = self.meme_dir / name
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

        self.index.pop(key, None)
        self._save_index()

        # 删除所有指向该 key 的别名
        bad = [a for a, k in self.alias.items() if k == key]
        for a in bad:
            self.alias.pop(a, None)
        if bad:
            self._save_alias()

        return True

    # ---------- commands ----------
    @filter.command("meme")
    async def meme_cmd(self, event: AstrMessageEvent):
        parts = (event.message_str or "").strip().split()
        action = parts[1].lower() if len(parts) >= 2 else "help"

        if action == "add":
            img = self._extract_first_image(event)
            if img is not None:
                ok, key_or_err = await self._add_from_image_segment(img)
                if ok:
                    alias = self._reverse_alias(key_or_err)
                    hint = f"（别名：{alias}）" if alias else f"\n可用：/meme name {key_or_err} <别名> 绑定别名"
                    yield event.plain_result(f"✅ 已收录表情包：{key_or_err}{hint}")
                else:
                    yield event.plain_result(f"❌ 收录失败：{key_or_err}")
                return

            gu = self._get_group_user_key(event)
            self.awaiting[gu] = time.time() + 60
            yield event.plain_result("好👌 现在请在 60 秒内发送一张表情包图片（直接发图即可，我会自动收录）")
            return

        if action == "name":
            if len(parts) < 4:
                yield event.plain_result("用法：/meme name <KEY> <别名>")
                return
            key = parts[2].strip().upper()
            alias = " ".join(parts[3:]).strip()

            if key not in self.index:
                yield event.plain_result(f"未找到该 KEY：{key}\n先用 /meme add 收录它")
                return

            self.alias[alias] = key
            self._save_alias()
            yield event.plain_result(f"✅ 已设置别名：{alias} -> {key}")
            return

        if action == "show":
            if len(parts) < 3:
                yield event.plain_result("用法：/meme show <KEY|别名>")
                return
            q = " ".join(parts[2:]).strip()
            key = self._resolve_key(q)
            if not key:
                yield event.plain_result(f"未找到：{q}")
                return
            name = self.index.get(key, "")
            alias = self._reverse_alias(key)
            yield event.plain_result(f"KEY: {key}\n别名: {alias or '（无）'}\n文件: {name or '（不存在）'}")
            return

        if action == "list":
            keys = sorted(self.index.keys())
            if not keys:
                yield event.plain_result("当前还没有收录任何表情包。用：/meme add")
                return
            lines = []
            for a, k in list(self.alias.items())[:10]:
                lines.append(f"{a} -> {k}")
            if len(lines) < 10:
                for k in keys:
                    if len(lines) >= 10:
                        break
                    if k in self.alias.values():
                        continue
                    lines.append(k)
            more = "" if len(keys) <= 10 else f"\n…共 {len(keys)} 个，仅显示部分"
            yield event.plain_result("已收录：\n" + "\n".join(lines) + more)
            return

        if action == "del":
            if len(parts) < 3:
                yield event.plain_result("用法：/meme del <KEY|别名>")
                return
            q = " ".join(parts[2:]).strip()
            key = self._resolve_key(q)
            if not key:
                yield event.plain_result(f"未找到：{q}")
                return
            if self._delete_key(key):
                yield event.plain_result(f"✅ 已删除：{q}（KEY={key}）")
            else:
                yield event.plain_result(f"删除失败：{q}")
            return

        if action == "reload":
            self._rebuild_index()
            bad = [a for a, k in self.alias.items() if k not in self.index]
            for a in bad:
                self.alias.pop(a, None)
            if bad:
                self._save_alias()
            yield event.plain_result(f"✅ 已重建索引，当前共 {len(self.index)} 个（清理无效别名 {len(bad)} 个）")
            return

        yield event.plain_result(
            "用法：\n"
            "/meme add               收录一张表情包\n"
            "/meme name <KEY> <别名> 绑定别名\n"
            "/meme show <KEY|别名>   查看\n"
            "/meme list              列表\n"
            "/meme del <KEY|别名>    删除\n"
            "/meme reload            重建索引"
        )

    # ---------- group message handler ----------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        # 等待收录模式
        gu = self._get_group_user_key(event)
        exp = self.awaiting.get(gu)
        if exp and time.time() <= exp:
            img = self._extract_first_image(event)
            if img is not None:
                ok, key_or_err = await self._add_from_image_segment(img)
                self.awaiting.pop(gu, None)
                if ok:
                    alias = self._reverse_alias(key_or_err)
                    hint = f"（别名：{alias}）" if alias else f"\n可用：/meme name {key_or_err} <别名> 绑定别名"
                    yield event.plain_result(f"✅ 已收录表情包：{key_or_err}{hint}")
                else:
                    yield event.plain_result(f"❌ 收录失败：{key_or_err}")
                event.stop_event()
                return
        elif exp and time.time() > exp:
            self.awaiting.pop(gu, None)

        # 命中复读
        msg = event.message_obj
        if not msg or not msg.message:
            return
        for seg in msg.message:
            if not isinstance(seg, Comp.Image):
                continue
            f = getattr(seg, "file", "") or ""
            key = Path(f).stem.upper()
            name = self.index.get(key)
            if not name:
                continue
            p = self.meme_dir / name
            if not p.exists():
                continue
            yield event.chain_result([Comp.Image.fromFileSystem(str(p))])
            event.stop_event()
            return

    # ---------- download / add ----------
    async def _add_from_image_segment(self, img: Comp.Image):
        # 1) 本地 path
        path = getattr(img, "path", "") or ""
        if path:
            p = Path(path)
            if p.exists() and p.is_file():
                data = p.read_bytes()
                ext = p.suffix or ".png"
                key = self._save_bytes_as_meme(data, ext)
                return True, key

        # 2) url 下载
        url = getattr(img, "url", None) or getattr(img, "src", None)
        if not url:
            return False, "图片段没有 url/path，无法获取原图数据"

        try:
            import aiohttp
        except Exception:
            return False, "缺少 aiohttp，无法下载图片。请安装：pip install aiohttp"

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return False, f"下载失败 HTTP {resp.status}"
                    data = await resp.read()
        except Exception as e:
            return False, f"下载异常：{e}"

        f = getattr(img, "file", "") or ""
        ext = (Path(f).suffix or ".png")
        key = self._save_bytes_as_meme(data, ext)
        return True, key