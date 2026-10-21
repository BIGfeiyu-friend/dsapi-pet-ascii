"""开发测试台：手动输入意图、请求生成、测试补丁、查看原始 JSON / 耗时 / token、保存与回放动画。"""
from __future__ import annotations

import json
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from . import face_rig as fr
from .ai import mock
from .config import AsciiConfig, data_dir
from .protocol import ProtocolError
from .render import AsciiView
from .runtime import Runtime

EXAMPLES = ["高兴地打招呼", "突然感到困惑", "变成一团乱码", "害羞地低下头，脸红了", "眯着眼偷笑", "生气地鼓起脸",
            "困得睁不开眼", "眨眨眼看向左边", "被夸奖后得意洋洋", "听到坏消息，眼泪在打转", "震惊到瞳孔地震",
            "举起手比个耶"]


def _sample_face(ctx) -> dict:
    c, eyes = mock.compose_face(ctx.face_w, ctx.face_h, "open", "smile", mock.random.Random(1), blush=True)
    c2, _ = mock.compose_face(ctx.face_w, ctx.face_h, "happy", "grin", mock.random.Random(2), blush=True)
    return {"mode": 2, "intent": "示例：微笑后眯眼", "expression": "开心", "ops": [
        {"type": "face_patch", "palette": mock.PALETTE, "eyes": eyes, "hold": "last",
         "frames": [c.frame(400), c2.frame(600)]}]}


def samples(ctx) -> dict:
    fw, fh = ctx.face_w, ctx.face_h
    return {
        "表情序列": {"mode": 2, "intent": "示例：先愣住，再笑出来", "expression": "大笑", "ops": [
            {"type": "face", "blend_ms": 200, "frames": [
                {"preset": "surprised", "ms": 400},
                {"preset": "laugh", "blush": 0.8, "ms": 900},
                {"eye_l": {"smile": 1.0}, "eye_r": {"open": 1.0, "smile": 0.2}, "mouth": {"shape": "tongue", "open": 0.4}, "ms": 700}]},
            {"type": "effect", "effect": "sparkle", "chars": "*+✦", "color": "#ffe27a", "duration_ms": 1600}]},
        "面部补丁": _sample_face(ctx),
        "差分更新": {"mode": 2, "intent": "示例：嘴巴张开", "ops": [
            {"type": "delta_update", "layer": "face", "palette": {"d": "#ff5d8f"},
             "changes": [{"x": fw // 2 - 1, "y": fh - 2, "text": "(O)", "colors": "ddd"}]}]},
        "区域补丁": {"mode": 2, "intent": "示例：身边举起一只手", "ops": [
            {"type": "region_patch", "anchor": "body", "layer": "overlay", "x": -6, "y": 20, "hold": "restore",
             "loop": "pingpong", "repeat": 3, "palette": {"s": "#ffe0d0", "o": "#2a3570"},
             "frames": [{"rows": [" \\\\ ", "  \\\\", "  |o"], "colors": [" ss ", "  ss", "  so"], "ms": 220},
                        {"rows": ["  ||", "  ||", "  |o"], "colors": ["  ss", "  ss", "  so"], "ms": 220}]}]},
        "全画布": {"mode": 3, "intent": "示例：解体后重组", "ops": [
            {"type": "effect", "effect": "glitch", "duration_ms": 1200, "intensity": 0.9},
            {"type": "full_scene", "hide": ["body", "face"], "then": "restore", "duration_ms": 2600,
             "rules": [{"effect": "dissolve", "intensity": 0.9}]},
            {"type": "effect", "effect": "flash", "delay_ms": 2500, "duration_ms": 500}]},
        "特效": {"mode": 1, "intent": "示例：开心地跳一下并冒星星", "ops": [
            {"type": "parameter_animation", "duration_ms": 700, "params": {"jump": 4, "blink": "twice"}},
            {"type": "effect", "effect": "sparkle", "chars": "*+✦", "color": "#ffe27a", "duration_ms": 1800},
            {"type": "effect", "effect": "bubble", "text": "嘿嘿~ 看我跳！", "duration_ms": 2200}]},
    }


class Lab(QMainWindow):
    def __init__(self, rt: Runtime | None = None):
        super().__init__()
        self._owns_rt = rt is None          # 只有自己建的 Runtime 才在关闭时停掉；和桌宠字符画窗口共用的不能停（否则那边的动画会卡住）
        self.rt = rt or Runtime()
        self.pet_window = None
        self.setWindowTitle(f"ASCII 桌宠 · 开发测试台 — {self.rt.char.display_name}")
        self.view = AsciiView(cell_h=self.rt.cfg.cell_h, glow=self.rt.cfg.glow, background=0xFF0A0D13,
                              plate_mode=self.rt.cfg.plate_mode)
        self.rt.add_view(self.view)
        self.rt.engine.tick(0.0)
        c, k, w, h, off = self.rt.engine.frame()
        self.view.set_frame(c, k, w, h, off)
        scroll = QScrollArea()
        scroll.setWidget(self.view)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setStyleSheet("QScrollArea { background: #0a0d13; border: none; }")
        split = QSplitter(Qt.Horizontal)
        split.addWidget(scroll)
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(460)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([self.view.sizeHint().width() + 20, 560])
        self.setCentralWidget(split)
        self._build_generate()
        self._build_patch()
        self._build_raw()
        self._build_state()
        self._build_history()
        self._build_custom()
        self._build_creative()
        self._build_persona()
        self._build_display()
        self.rt.listeners.append(self._on_result)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_state)
        self.timer.timeout.connect(self._persona_tick)
        self.timer.start(500)
        self.resize(min(1600, self.view.sizeHint().width() + 640), max(720, self.view.sizeHint().height() + 40))

    # ------------------------------------------------------------------ 生成
    def _build_generate(self):
        w = QWidget()
        f = QFormLayout(w)
        self.intent = QLineEdit()
        self.intent.setPlaceholderText("输入表达意图，例如：高兴地打招呼 / 突然感到困惑 / 变成一团乱码")
        self.intent.returnPressed.connect(lambda: self._gen())
        ex = QComboBox()
        ex.addItems(["（示例意图）"] + EXAMPLES)
        ex.activated.connect(lambda i: i and self.intent.setText(EXAMPLES[i - 1]))
        self.context = QLineEdit()
        self.context.setPlaceholderText("可选：对话语境摘要")
        self.intensity = QSlider(Qt.Horizontal)
        self.intensity.setRange(0, 100)
        self.intensity.setValue(60)
        btns, btns2 = QHBoxLayout(), QHBoxLayout()
        for idx, (label, kw) in enumerate((("生成（自动选模式）", {}), ("强制生成新的", {"force_new": True}),
                                           ("Mode 1 参数", {"modes": (1,)}), ("Mode 2 局部", {"modes": (2,)}),
                                           ("Mode 3 全画布", {"modes": (3,)}))):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kw: self._gen(**k))
            (btns if idx < 2 else btns2).addWidget(b)
        self.mock_box = QCheckBox("Mock 模式（不调用 API）")
        self.mock_box.setChecked(self.rt.director.client_desc.startswith("mock"))
        self.mock_box.toggled.connect(self._toggle_mock)
        self.client_label = QLabel(self.rt.director.client_desc)
        self.client_label.setWordWrap(True)
        self.gen_status = QLabel("")
        self.gen_status.setWordWrap(True)
        f.addRow("表达意图", self.intent)
        f.addRow("", ex)
        f.addRow("对话语境", self.context)
        from .face_rig import PRESETS

        presets = list(PRESETS)
        pre = QComboBox()
        pre.addItems(["（选择后立即切换，不调用 API）"] + presets)
        pre.activated.connect(lambda i: i and self.rt.apply_preset(presets[i - 1]))
        pre.setEnabled(self.rt.engine.rigged)
        f.addRow("本地表情预设", pre)
        # 睡眠测试：直接入睡 / 吵醒（不用真的等 30 分钟）
        sl = self.rt.sleep
        zz = QHBoxLayout()
        for label, v in (("入睡（随机）", None), ("打哈欠", "yawn"), ("点头", "nod"), ("晚安", "goodnight")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, vv=v: sl.fall_asleep(vv))
            b.setEnabled(self.rt.engine.rigged)
            zz.addWidget(b)
        wk = QComboBox()
        wake_names = [("startle", "吓一跳"), ("groggy", "迷迷糊糊"), ("grumpy", "起床气"), ("drool", "流口水被发现"),
                      ("glitch", "死机重启"), ("stretch", "伸懒腰")]
        wk.addItems(["吵醒（随机）"] + [f"吵醒：{n}" for _, n in wake_names])
        wk.activated.connect(lambda i: sl.wake("chat", wake_names[i - 1][0] if i else None))
        zz.addWidget(wk)
        f.addRow("睡眠", zz)
        f.addRow("表达强度", self.intensity)
        f.addRow(btns)
        f.addRow(btns2)
        f.addRow(self.mock_box)
        f.addRow("当前模型", self.client_label)
        f.addRow("结果", self.gen_status)
        self.tabs.addTab(w, "生成")

    def _gen(self, force_new=False, modes=None):
        text = self.intent.text().strip()
        if not text:
            return
        req = self.rt.express(text, source="lab", priority=8, context=self.context.text().strip(),
                              force_new=force_new or modes is not None, modes=modes,
                              intensity=self.intensity.value() / 100)
        self.gen_status.setText("已提交，等待生成…（期间待机动画继续）" if req else self.rt.director.status)

    def _toggle_mock(self, on):
        self.client_label.setText(self.rt.use_mock(on))

    # ------------------------------------------------------------------ 补丁测试
    def _build_patch(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.patch_edit = QPlainTextEdit()
        self.patch_edit.setStyleSheet("font-family: Consolas; font-size: 12px;")
        for name in ("表情序列", "面部补丁", "差分更新", "区域补丁", "全画布", "特效"):
            b = QPushButton(name + "示例")
            b.clicked.connect(lambda _=False, n=name: self.patch_edit.setPlainText(
                json.dumps(samples(self.rt.engine.context())[n], ensure_ascii=False, indent=1)))
            row.addWidget(b)
        apply = QPushButton("应用 JSON")
        apply.clicked.connect(self._apply_patch)
        self.patch_msg = QLabel("")
        self.patch_msg.setWordWrap(True)
        v.addLayout(row)
        v.addWidget(self.patch_edit)
        v.addWidget(apply)
        v.addWidget(self.patch_msg)
        first = "表情序列" if self.rt.engine.rigged else "面部补丁"
        self.patch_edit.setPlainText(json.dumps(samples(self.rt.engine.context())[first], ensure_ascii=False, indent=1))
        self.tabs.addTab(w, "补丁测试")

    def _apply_patch(self):
        try:
            anim = self.rt.apply_json(self.patch_edit.toPlainText())
            self.patch_msg.setText(f"已应用：{anim.summary()}" + (f"\n警告：{anim.warnings}" if anim.warnings else ""))
        except (ProtocolError, ValueError) as exc:
            self.patch_msg.setText(f"无效：{exc}")

    # ------------------------------------------------------------------ 原始 JSON / 状态
    def _build_raw(self):
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setStyleSheet("font-family: Consolas; font-size: 12px;")
        self.tabs.addTab(self.raw, "原始 JSON")

    def _build_state(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.state_json = QPlainTextEdit()
        self.state_json.setReadOnly(True)
        self.state_json.setStyleSheet("font-family: Consolas; font-size: 11px;")
        v.addWidget(self.state_label)
        v.addWidget(QLabel("发送给模型的视觉状态（压缩后）："))
        v.addWidget(self.state_json)
        self.tabs.addTab(w, "状态")

    def _on_result(self, res):
        s = res.summary()
        flags = []
        if s["cached"]:
            flags.append("复用缓存")
        if s["fallback"]:
            flags.append("本地兜底")
        self.gen_status.setText(
            f"[{s['time']}] {s['intent']}\n模式 {s['mode']}  操作 {', '.join(s['ops'])}  耗时 {s['elapsed']}s  "
            f"token 提示/输出/缓存命中 = {s['prompt_tokens']}/{s['completion_tokens']}/{s['cache_hit_tokens']}  "
            f"{' '.join(flags)}" + (f"\n错误：{s['error']}" if s["error"] else "")
            + (f"\n警告：{s['warnings']}" if s["warnings"] else ""))
        self.raw.setPlainText(f"// {s['time']} {s['model']} 第 {s['attempts']} 次尝试\n" + (res.raw_text or ""))
        self._refresh_history()

    def _refresh_state(self):
        e = self.rt.engine
        b = self.rt.store.budget
        last = self.rt.director.last
        cur = e.current
        self.state_label.setText(
            f"当前动画：{cur.summary() if cur else '待机（本地）'}\n"
            f"调度：{self.rt.director.status}   排队中：{'是' if self.rt.director.busy else '否'}\n"
            f"上次请求：{(f'{last.elapsed:.2f}s，' + str(last.usage)) if last else '无'}\n"
            f"今日预算：请求 {b.requests}/{self.rt.cfg.daily_request_limit}，token {b.tokens}/{self.rt.cfg.daily_token_limit}"
            f"   累计 token {b.total_tokens}\n"
            f"表情：{e.expression}   最近：{e.recent[-5:]}\n"
            f"对话表情标签：{(self.rt.last_inline or {}).get('tag', '无')}（共 {self.rt.inline_count} 次，联动方式 {self.rt.cfg.chat_link}）\n"
            f"说完之后：{self._idle_text()}\n"
            f"睡眠：{self._sleep_text()}\n"
            f"FPS {self.rt.fps:.1f}   合成 {e.stats['compose_ms']:.1f}ms   画布 {e.W}x{e.H}")
        if self.tabs.currentIndex() == 3:
            self.state_json.setPlainText(json.dumps(e.visual_state(), ensure_ascii=False, indent=1))

    def _sleep_text(self) -> str:
        sl = self.rt.sleep
        if sl.state == "awake":
            left = max(0.0, self.rt.cfg.sleep_after_min * 60 - sl.idle_seconds())
            return (f"醒着，{left / 60:.1f} 分钟后无互动就睡" if self.rt.cfg.sleep_enabled else "醒着（已关闭自动入睡）")
        last = sl.history[-1] if sl.history else None
        beat = f"，第 {sl.beats} 拍：{last['mood']} × {last['breaths']} 口呼吸 {last['events']}" if last else ""
        return f"{sl.status_text()}（梦境 {sl.mood}{beat}）"

    def _idle_text(self) -> str:
        li = self.rt.last_idle
        if not li:
            return "模型还没有做过决定（回复末尾没有收尾标签时保持当前表情）"
        wait = f"说完 {li.get('delay', li['after']):.0f}s 后（1 分钟起步 + 她决定的 {li['after']:.0f}s）"
        if li["decision"] == "keep":
            s = f"{li['time']} 决定{wait}保持当前表情进入挂机"
        else:
            s = f"{li['time']} 决定{wait}换成「{li['decision']}」"
        s += f"，{li['fired']} 已进入挂机" if li.get("fired") else ("" if li.get("cancelled") else "，等待中")
        if li.get("cancelled"):
            s += f"（已取消：{li['cancelled']}）"
        return s + ("   【挂机中】" if self.rt.idle_state else "")

    # ------------------------------------------------------------------ 历史
    def _build_history(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.hist = QListWidget()
        row = QHBoxLayout()
        for label, fn in (("回放", self._replay), ("删除", self._delete), ("刷新", self._refresh_history),
                          ("另存最近一次", self._save_last)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.hist_msg = QLabel("")
        v.addWidget(self.hist)
        v.addLayout(row)
        v.addWidget(self.hist_msg)
        self.tabs.addTab(w, "历史动画")
        self._refresh_history()

    def _refresh_history(self):
        self.hist.clear()
        for e in self.rt.cache.recent(80):
            t = time.strftime("%m-%d %H:%M", time.localtime(e["created"]))
            it = QListWidgetItem(f"{t}  M{e['mode']}  {e['intent'][:28]}  → {e['expression'][:12]}  "
                                 f"[{e.get('model', '')}] {e.get('tokens', 0)}tok  用过{e.get('uses', 0)}次")
            it.setData(Qt.UserRole, e["id"])
            self.hist.addItem(it)

    def _replay(self):
        it = self.hist.currentItem()
        if it:
            anim = self.rt.replay(it.data(Qt.UserRole))
            self.hist_msg.setText(f"回放：{anim.summary() if anim else '读取失败'}")

    def _delete(self):
        it = self.hist.currentItem()
        if it:
            self.rt.cache.delete(it.data(Qt.UserRole))
            self._refresh_history()

    def _save_last(self):
        last = self.rt.director.last
        if not last or not last.anim:
            self.hist_msg.setText("还没有生成结果")
            return
        d = data_dir() / "saved"
        d.mkdir(exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + "_" + "".join(c for c in last.request.intent[:16] if c.isalnum())
        p = d / f"{name}.json"
        p.write_text(json.dumps(last.anim.raw, ensure_ascii=False, indent=1), encoding="utf-8")
        self.hist_msg.setText(f"已保存：{p}")

    # ------------------------------------------------------------------ 自创表情
    def _build_custom(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.custom_list = QListWidget()
        self.custom_list.itemDoubleClicked.connect(lambda it: self._custom_preview())
        rows = []
        for group in ((("预览", self._custom_preview), ("👍", lambda: self._custom_rate(1)), ("👎", lambda: self._custom_rate(-1)),
                       ("回滚上一版", self._custom_rollback), ("恢复", self._custom_restore)),
                      (("重命名", self._custom_rename), ("删除", self._custom_delete), ("立即进化", self._custom_evolve),
                       ("清空对话素材", self._custom_clear_dialogue), ("刷新", self._refresh_custom))):
            row = QHBoxLayout()
            for label, fn in group:
                b = QPushButton(label)
                b.clicked.connect(fn)
                row.addWidget(b)
            rows.append(row)
        self.custom_info = QLabel("")
        self.custom_info.setWordWrap(True)
        self.custom_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(QLabel("表情库：DeepSeek 自创的表情、表情进化改良过的内置表情、已淘汰的表情。双击预览；"
                           "选中后可以 👍/👎、回滚到上一版、恢复已淘汰或恢复内置原版。"))
        v.addWidget(self.custom_list)
        for row in rows:
            v.addLayout(row)
        v.addWidget(self.custom_info)
        self.tabs.addTab(w, "自创表情")
        self.rt.listeners.append(lambda _res: self._refresh_custom())
        self._refresh_custom()

    def _refresh_custom(self):
        self.custom_list.clear()
        origin_cn = {"chat": "对话即兴", "evolve": "表情进化", "mirror": "照镜子微调", "generator": "生成器",
                     "lab": "测试台", "rollback": "回滚", "system": "系统"}
        kind_cn = {"custom": "", "builtin": "【改良内置】", "retired": "【已淘汰】"}
        for it in self.rt.custom.snapshot():
            t = time.strftime("%m-%d %H:%M", time.localtime(it.get("created", 0)))
            name = it["name"]
            if it["kind"] == "builtin":
                name = f"{fr.display_name(name)}({name})"
            extra = ""
            ch = it.get("last_change") or {}
            if ch.get("diff") is not None and it["kind"] != "retired":
                extra = f"  上次变化 {ch['diff']:.0%}" + (f"（放大×{ch['gain']:g}）" if ch.get("gain", 1) > 1 else "")
            if it["kind"] == "retired":
                extra = f"  原因：{it.get('retire_reason', '')}" + (f"（并入「{it['merged_into']}」）" if it.get("merged_into") else "")
            text = (f"{kind_cn[it['kind']]}{name}  [{origin_cn.get(it.get('origin'), it.get('origin'))}] "
                    f"用过{it.get('uses', 0)}次 👍{it.get('likes', 0)} 👎{it.get('dislikes', 0)} 第{it.get('versions', 1)}版  {t}"
                    f"  {it.get('note', '')}" + (f"  ——「{it['context'][:24]}」" if it.get("context") else "") + extra)
            li = QListWidgetItem(text)
            li.setData(Qt.UserRole, (it["name"], it["kind"]))
            li.setToolTip("照镜子：" + it.get("looks", "") + "\n" + json.dumps(it["spec"], ensure_ascii=False))
            self.custom_list.addItem(li)
        lc, le = self.rt.last_created, self.rt.last_evolve
        lib = self.rt.custom
        lines = [f"共 {len(lib.items)} 个自创表情（上限 {self.rt.cfg.custom_presets_max}），已淘汰 {len(lib.retired)} 个，"
                 f"别名 {len(lib.aliases)} 个；当前表情：{fr.display_name(self.rt.current_expression())}；"
                 f"变化幅度门槛 {lib.min_change:.0%}（不够自动放大，最多 ×{lib.max_gain:g}）"]
        if lc:
            lines.append(f"最近一次对话自创：{lc['time']} 「{lc['name']}」→「{lc['final']}」（{lc['status']}）")
        if le:
            if le.get("error"):
                lines.append(f"最近一次表情进化：{le.get('time')} 失败：{le['error']}")
            else:
                op_cn = {"create": "新建", "revise": "修改", "merge": "合并", "retire": "淘汰"}
                res = "、".join(f"{op_cn.get(r.get('op', 'create'), '')}{r['final']}({r['status']}"
                               + (f"，变化{r['change']}" if r.get("change") else "") + ")"
                               for r in le.get("results", [])) or "没有改动"
                lines.append(f"最近一次表情进化：{le.get('time')} {le.get('elapsed', 0)}s  {le.get('thoughts', '')}  → {res}")
                if le.get("checks"):
                    lines.append("照镜子复查：" + "、".join(
                        f"{c['name']} {'到位' if c['ok'] else '再修了一次：' + c.get('why', '')}" for c in le["checks"]))
        st = self.rt._evolve_state()
        lines.append(f"对话素材：{len(self.rt.dialogue)} 轮（上次进化后新增 {self.rt.dialogue.since_evolve} 轮，"
                     f"满 {self.rt.cfg.evolve_min_replies} 轮、间隔 {self.rt.cfg.evolve_interval_min:g} 分钟后自动进化；"
                     f"今天已进化 {st.get('count', 0)}/{self.rt.cfg.evolve_daily_limit} 次）")
        self.custom_info.setText("\n".join(lines))

    def _toggle_island(self, on: bool) -> None:
        from . import integration

        if integration._state.get("shell") is not None and integration._state.get("rt") is self.rt:
            integration.set_island_integrated(on)       # 随桌宠运行：立即生效
        else:
            self.rt.cfg.integrate_island = on
            self.rt.cfg.save()

    def _set_chat_skin(self, name: str) -> None:
        from . import chat_theme

        self.rt.cfg.chat_theme = name
        self.rt.cfg.save()
        if chat_theme._state["installed"]:
            chat_theme.set_skin(name)

    def _custom_selected(self):
        it = self.custom_list.currentItem()
        return it.data(Qt.UserRole) if it else (None, None)

    def _custom_preview(self):
        name, kind = self._custom_selected()
        if name and kind != "retired":
            self.rt.apply_preset(name)

    def _custom_rate(self, vote: int):
        name, kind = self._custom_selected()
        if name and kind != "retired":
            self.rt.apply_preset(name)
            self.rt.custom.rate(name, vote, context="（在测试台评价）")
        elif not name:
            self.rt.rate_current(vote)
        self._refresh_custom()

    def _custom_rollback(self):
        name, kind = self._custom_selected()
        if not name or kind == "retired":
            return
        if not self.rt.custom.rollback(name):
            self.custom_info.setText("没有更早的版本可以回滚")
            return
        self.rt.apply_preset(name)
        self._refresh_custom()

    def _custom_restore(self):
        name, kind = self._custom_selected()
        if kind == "retired":
            self.rt.custom.restore(name)
        elif kind == "builtin":
            self.rt.custom.reset_builtin(name)
        self._refresh_custom()

    def _custom_rename(self):
        from PySide6.QtWidgets import QInputDialog

        name, kind = self._custom_selected()
        if not name or kind != "custom":
            return
        new, ok = QInputDialog.getText(self, "重命名自创表情", "新名字（2~12 字，不能与内置预设重名）", text=name)
        if ok and new.strip() and not self.rt.custom.rename(name, new.strip()):
            self.custom_info.setText("重命名失败：名字不合法或已存在")
        self._refresh_custom()

    def _custom_delete(self):
        name, kind = self._custom_selected()
        if name and kind in ("custom", "retired"):
            self.rt.custom.delete(name)
            self._refresh_custom()

    def _custom_clear_dialogue(self):
        self.rt.clear_dialogue()
        self._refresh_custom()

    def _custom_evolve(self):
        if not len(self.rt.dialogue):
            self.custom_info.setText("还没有对话素材：表情进化需要先和桌宠聊几句（素材存在字符画数据目录，不读取桌宠的记忆）。")
            return
        started = self.rt.evolve_now(client=self.rt.director.client if self.rt.director.client_desc.startswith("mock") else None)
        self.custom_info.setText("表情进化已在后台开始……" if started else "表情进化正在进行或预算不足")

    # ------------------------------------------------------------------ 创作（v0.7）
    def _build_creative(self):
        from .creative import KIND_CN, KINDS
        from .parts import STYLE_CN, STYLES

        w = QWidget()
        v = QVBoxLayout(w)
        self.cr_status = QLabel("")
        self.cr_status.setWordWrap(True)
        self.cr_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.cr_status)
        row = QHBoxLayout()
        self.cr_style = QComboBox()
        self.cr_style.addItem("风格：自动（探索 / 利用）", None)
        for s in STYLES:
            self.cr_style.addItem(f"风格：{STYLE_CN[s]}", s)
        self.cr_kind = QComboBox()
        self.cr_kind.addItem("类型：自动", None)
        for k in KINDS:
            self.cr_kind.addItem(f"类型：{KIND_CN[k]}", k)
        start = QPushButton("开始一次实验")
        start.clicked.connect(self._cr_start)
        stop = QPushButton("取消")
        stop.clicked.connect(self.rt.cancel_experiment)
        for x in (self.cr_style, self.cr_kind, start, stop):
            row.addWidget(x)
        v.addLayout(row)
        v.addWidget(QLabel("实验记录（候选设计只在这里，采用后才进正式库）："))
        self.cr_exps = QListWidget()
        self.cr_exps.currentRowChanged.connect(lambda _i: self._cr_show_exp())
        v.addWidget(self.cr_exps, 2)
        self.cr_detail = QPlainTextEdit()
        self.cr_detail.setReadOnly(True)
        v.addWidget(self.cr_detail, 2)
        v.addWidget(QLabel("正式组件库 / 自由演出（双击预览）："))
        self.cr_parts = QListWidget()
        self.cr_parts.itemDoubleClicked.connect(lambda _it: self._cr_preview())
        v.addWidget(self.cr_parts, 2)
        row2 = QHBoxLayout()
        for label, fn in (("预览", self._cr_preview), ("淘汰", lambda: self._cr_part_action("retire")),
                          ("恢复", lambda: self._cr_part_action("restore")), ("回滚上一版", lambda: self._cr_part_action("rollback")),
                          ("刷新", self._refresh_creative)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row2.addWidget(b)
        v.addLayout(row2)
        self.cr_notes = QLabel("")
        self.cr_notes.setWordWrap(True)
        v.addWidget(self.cr_notes)
        self.tabs.addTab(w, "创作")
        self.rt.experiment_done.connect(lambda _r: self._refresh_creative())
        self._cr_timer = QTimer(self)
        self._cr_timer.timeout.connect(self._refresh_creative_status)
        self._cr_timer.start(1000)
        self._refresh_creative()

    def _cr_start(self):
        client = self.rt.director.client if self.rt.director.client_desc.startswith("mock") else None
        ok = self.rt.experiment_now(self.cr_style.currentData(), self.cr_kind.currentData(), client=client)
        self.cr_detail.setPlainText("创作实验已在后台开始……（设计 → 本地渲染检查 → 视觉评价（如果可用）→ 她自己决定）"
                                    if ok else "已有实验在进行")

    def _refresh_creative_status(self):
        if self.tabs.tabText(self.tabs.currentIndex()) != "创作":
            return
        rt = self.rt
        vs = rt.vision.status()
        cr = rt.creative._today()
        lines = [f"实验：{rt.experiment_status()}；今天 {cr.get('runs', 0)}/{rt.cfg.experiment_daily_limit} 次、"
                 f"{cr.get('tokens', 0)}/{rt.cfg.experiment_daily_tokens} token；"
                 f"自动实验 {'开' if rt.cfg.experiments else '关'}（没人理她 {rt.cfg.experiment_idle_min:g} 分钟后 / 睡着时，"
                 f"间隔 ≥{rt.cfg.experiment_interval_min:g} 分钟）",
                 f"视觉评价：{'可用' if vs['available'] else '不可用（' + vs['why'] + '）'}"
                 + (f"，模型 {vs['model']}（{vs['source']}）" if vs["model"] else "")
                 + f"；今天 {vs['requests']}/{rt.cfg.vision_daily_requests} 次、{vs['tokens']} token；排队 {vs['pending']}"
                 + ("，正在评价" if vs["running"] else "") + (f"；上次失败：{vs['last_error']}" if vs["last_error"] else ""),
                 "耗时：" + rt.latency.text()]
        self.cr_status.setText("\n".join(lines))

    def _refresh_creative(self):
        from .creative import KIND_CN
        from .parts import STYLE_CN

        self._refresh_creative_status()
        rt = self.rt
        self.cr_exps.clear()
        outcome_cn = {"adopted": "✔ 采用", "abandoned": "✘ 放弃", "error": "⚠ 出错", "cancelled": "取消", "running": "进行中"}
        self._cr_exp_data = list(reversed(rt.creative.snapshot(40)))
        for e in self._cr_exp_data:
            t = time.strftime("%m-%d %H:%M", time.localtime(e.get("time", 0)))
            self.cr_exps.addItem(f"{t}  {outcome_cn.get(e.get('status'), e.get('status'))}  {STYLE_CN.get(e['style'], e['style'])}·"
                                 f"{KIND_CN.get(e['kind'], e['kind'])}  {e.get('name') or ''}  {e.get('intent', '')[:20]}"
                                 + (f"  ——{e['failure'][:30]}" if e.get("failure") else ""))
        self.cr_parts.clear()
        for row in rt.parts.summary(60, include_retired=True):
            li = QListWidgetItem(f"{'' if row['status'] == 'active' else '【已淘汰】'}部件「{row['name']}」 {row['slot']} · "
                                 f"{STYLE_CN.get(row['style'], row['style'])}  用过{row['uses']}次 👍{row['likes']} 👎{row['dislikes']} "
                                 f"第{row['versions']}版  {row['intent'][:24]}" + (f"  视觉：{row['vision'][:30]}" if row.get("vision") else ""))
            li.setData(Qt.UserRole, ("part", row["name"]))
            self.cr_parts.addItem(li)
        for name, s in rt.creative.data.get("scenes", {}).items():
            li = QListWidgetItem(f"{'' if s.get('status', 'active') == 'active' else '【已淘汰】'}演出「{name}」 "
                                 f"{STYLE_CN.get(s.get('style'), s.get('style'))}  播放{s.get('uses', 0)}次  {s.get('intent', '')[:24]}")
            li.setData(Qt.UserRole, ("scene", name))
            self.cr_parts.addItem(li)
        notes = [n["text"] for n in rt.creative.data.get("notes", [])[-6:]]
        styles = rt.creative.data.get("styles", {})
        self.cr_notes.setText(("她的创作心得：" + "；".join(notes) if notes else "还没有创作心得")
                              + "\n风格统计：" + ("；".join(f"{STYLE_CN.get(s, s)} 试{v.get('tries', 0)}/采用{v.get('adopted', 0)}"
                                                         for s, v in styles.items()) or "暂无")
                              + "\n代表作（进化不淘汰）：" + ("、".join(f"{n}（{STYLE_CN.get(s, s)}）" for n, s in rt.custom.protected.items()) or "暂无"))

    def _cr_show_exp(self):
        i = self.cr_exps.currentRow()
        data = getattr(self, "_cr_exp_data", [])
        if not 0 <= i < len(data):
            return
        e = data[i]
        lines = [f"目标：{e['style']} / {e['kind']}（{e.get('why', '')}）  结果：{e.get('status')}  {e.get('name', '')}",
                 f"意图：{e.get('intent', '')}"]
        if e.get("failure"):
            lines.append(f"失败原因：{e['failure']}")
        for it in e.get("iterations", []):
            lines.append(f"—— 第 {it.get('round')} 轮 —— 决定：{it.get('decision', '?')}  {it.get('reason', '')}")
            if it.get("local_errors"):
                lines.append("  本地检查·错误：" + "；".join(it["local_errors"]))
            if it.get("local_warnings"):
                lines.append("  本地检查·提醒：" + "；".join(it["local_warnings"][:5]))
            if it.get("looks"):
                lines.append("  实际样子：" + it["looks"][:160])
            if it.get("vision"):
                lines.append("  视觉评价：" + str(it["vision"].get("summary", ""))[:160])
            if it.get("validation"):
                lines.append("  被程序忽略 / 钳制：" + "；".join(it["validation"][:4]))
            lines.append("  设计：" + json.dumps(it.get("design", {}), ensure_ascii=False)[:600])
        if e.get("adopted"):
            lines.append("登记进正式库：" + json.dumps(e["adopted"], ensure_ascii=False))
        self.cr_detail.setPlainText("\n".join(lines))

    def _cr_selected(self):
        it = self.cr_parts.currentItem()
        return it.data(Qt.UserRole) if it is not None else (None, None)

    def _cr_preview(self):
        kind, name = self._cr_selected()
        if kind == "scene":
            self.rt.play_scene(name)
            return
        comp = self.rt.parts.items.get(name)
        if not comp:
            return
        slot = comp.get("slot")
        if slot == "eye":
            spec = {"parts": {"eye_l": name, "eye_r": name}}
        elif slot in ("mouth", "forehead"):
            spec = {"parts": {slot: name}}
        elif slot == "brow":
            spec = {"parts": {"brow_l": name, "brow_r": name}}
        elif slot == "cheek":
            spec = {"parts": {"cheek_l": name, "cheek_r": name}}
        else:
            spec = {"extra": [{"id": "preview", "x": 0.5, "y": 0.45, "w": 0.12, "h": 0.1, "part": name}]}
        if comp.get("status") != "active":
            self.cr_detail.setPlainText("已淘汰的部件要先恢复才能预览")
            return
        self.rt.engine.set_expression(spec, 300)

    def _cr_part_action(self, action):
        kind, name = self._cr_selected()
        if not name:
            return
        if kind == "scene":
            if action == "retire":
                self.rt.creative.retire_scene(name)
        else:
            getattr(self.rt.parts, action)(name)
        self._refresh_creative()

    # ------------------------------------------------------------------ 显示
    # ------------------------------------------------------------------ 副人格（v0.9）
    def _build_persona(self):
        from .persona import MODE_CN, MODES

        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        self.persona_mode = QComboBox()
        for m in MODES:
            self.persona_mode.addItem(MODE_CN[m], m)
        mode = self.rt.cfg.persona_mode
        self.persona_mode.setCurrentIndex(MODES.index(mode) if mode in MODES else 0)
        self.persona_mode.activated.connect(self._persona_set_mode)
        self.persona_frozen = QCheckBox("冻结（只读，不再演化）")
        self.persona_frozen.setChecked(self.rt.persona.frozen)
        self.persona_frozen.toggled.connect(self._persona_freeze)
        top.addWidget(QLabel("模式"))
        top.addWidget(self.persona_mode, 1)
        top.addWidget(self.persona_frozen)
        v.addLayout(top)
        self.persona_info = QLabel("")
        self.persona_info.setWordWrap(True)
        self.persona_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.persona_info)

        def buttons(items):
            row = QHBoxLayout()
            for label, fn in items:
                b = QPushButton(label)
                b.clicked.connect(fn)
                row.addWidget(b)
            return row

        v.addWidget(QLabel("条目（🔒 = 你保护的；强度只给你看，她看不到数字和历史）："))
        self.persona_entries = QListWidget()
        self.persona_entries.currentItemChanged.connect(lambda *_: self._persona_detail("entry"))
        v.addWidget(self.persona_entries, 3)
        v.addLayout(buttons((("保护 / 取消保护", self._persona_protect), ("撤销最近一次变化", self._persona_rollback),
                             ("恢复", self._persona_restore), ("删除", self._persona_delete))))
        v.addWidget(QLabel("等你批准 / 候选池（攒证据中、待审核、等速度上限）："))
        self.persona_pool = QListWidget()
        self.persona_pool.currentItemChanged.connect(lambda *_: self._persona_detail("cand"))
        v.addWidget(self.persona_pool, 2)
        v.addLayout(buttons((("同意（立即生效）", self._persona_approve), ("拒绝", self._persona_reject),
                             ("立即反思（调试用）", self._persona_reflect), ("清空影子结果", self._persona_clear),
                             ("刷新", self._refresh_persona))))
        self.persona_detail = QPlainTextEdit()
        self.persona_detail.setReadOnly(True)
        self.persona_detail.setStyleSheet("font-size: 12px;")
        v.addWidget(QLabel("详情 / 审计日志（点空白处看审计日志）："))
        v.addWidget(self.persona_detail, 3)
        self.tabs.addTab(w, "人格")
        self._persona_seen = None
        self._persona_busy = None
        self._refresh_persona()

    def _persona_tick(self):
        if self.rt.last_reflect is not self._persona_seen or self.rt._reflecting != self._persona_busy:
            self._persona_seen = self.rt.last_reflect
            self._persona_busy = self.rt._reflecting
            self._refresh_persona()

    def _refresh_persona(self):
        from .persona import CAND_CN, MODE_CN, OP_CN, STATUS_CN

        p, cfg = self.rt.persona, self.rt.cfg
        p.reload_if_changed()
        keep_e = self._persona_sel(self.persona_entries)
        keep_c = self._persona_sel(self.persona_pool)
        self.persona_entries.blockSignals(True)
        self.persona_entries.clear()
        order = {"active": 0, "trial": 1, "fading": 2, "paused": 3, "retired": 4}
        for e in sorted(p.entries(), key=lambda e: (order.get(e["status"], 9), -e["strength"])):
            n = round(e["strength"] * 10)
            text = (f"{'🔒' if e.get('protected') else '  '} {e['id']} [{STATUS_CN.get(e['status'], e['status'])}] "
                    f"{'▮' * n}{'▯' * (10 - n)} {e['strength']:.2f}  {e['kind']}  {e['text']}  "
                    f"（证据 {len(e.get('evidence', []))} 条）" + ("  〔影子〕" if e.get("phase") == "shadow" else ""))
            li = QListWidgetItem(text)
            li.setData(Qt.UserRole, e["id"])
            self.persona_entries.addItem(li)
            if e["id"] == keep_e:
                self.persona_entries.setCurrentItem(li)
        self.persona_entries.blockSignals(False)
        self.persona_pool.blockSignals(True)
        self.persona_pool.clear()
        rank = {"pending_approval": 0, "reviewing": 1, "deferred": 2, "accumulating": 3}
        for c in sorted(p.data["pool"], key=lambda c: (rank.get(c["status"], 9), -c.get("updated", 0))):
            what = c.get("text") or c.get("target", "")
            if c["op"] == "merge":
                what = f"{c.get('target')} 并入 {c.get('into')}" + (f"：{c['text']}" if c.get("text") else "")
            elif c["op"] != "create" and c.get("target"):
                what = c["target"] + (f"：{c['text']}" if c.get("text") else "")
            li = QListWidgetItem(f"{'⚑ ' if c['status'] == 'pending_approval' else ''}{c['id']} "
                                 f"[{CAND_CN.get(c['status'], c['status'])}] {OP_CN.get(c['op'], c['op'])} {what}  — "
                                 + (c.get("why") or p.ready(c)[1]))
            li.setData(Qt.UserRole, c["id"])
            self.persona_pool.addItem(li)
            if c["id"] == keep_c:
                self.persona_pool.setCurrentItem(li)
        self.persona_pool.blockSignals(False)
        b = p.budget_state(cfg.persona_max_entries, cfg.persona_max_chars)
        r = p.data["reflect"]
        last = time.strftime("%m-%d %H:%M", time.localtime(r["last"])) if r.get("last") else "还没有"
        used = cfg.persona_daily_tokens - p.tokens_left(cfg.persona_daily_tokens)
        lines = [f"模式：{MODE_CN.get(cfg.persona_mode, cfg.persona_mode)}" + ("（冻结中）" if p.frozen else "")
                 + f"；条目 {b['entries']}/{b['max_entries']}，进提示词的字数 {b['chars']}/{b['max_chars']}；"
                 f"今天副人格用了 {used}/{cfg.persona_daily_tokens} token",
                 f"上次完整的夜间反思：{last}；已读到第 {r.get('cursor', 0)} 条消息"
                 + ("（还有没读完的，下次接着读）" if r.get("backlog") else "")
                 + f"。她睡着 {cfg.persona_after_sleep_min:g} 分钟后、距上次 {cfg.persona_reflect_min_hours:g} 小时以上、"
                 f"新消息里有你说的 {cfg.persona_min_user_messages} 句以上才会反思",
                 "正典（桌宠设置里的人设）：" + (f"已记下（{len(p.data['canon'].get('text', ''))} 字）"
                                         if p.data["canon"].get("hash") else "还没记下：和她聊一句之后才知道")]
        if self.rt._reflecting:
            lines.append("夜间反思进行中……")
        lr = self.rt.last_reflect
        if lr:
            if lr.get("error"):
                lines.append(f"最近一次反思：{lr.get('time_str') or lr.get('time', '')} 失败 / 没有进行：{lr['error']}")
            else:
                lines.append(f"最近一次反思：{lr.get('time_str', '')} {lr.get('elapsed', 0)}s，{lr.get('tokens', 0)} token，"
                             f"读了 {lr.get('messages', 0)} 条消息。{lr.get('skipped') or lr.get('thoughts', '')}")
        self.persona_info.setText("\n".join(lines))
        if not self.persona_entries.currentItem() and not self.persona_pool.currentItem():
            self.persona_detail.setPlainText(self._persona_audit_text())

    def _persona_audit_text(self, n: int = 80) -> str:
        names = {"reflect": "夜间反思", "reflect_skip": "跳过反思", "propose": "新观察", "support": "补充证据",
                 "support_miss": "补充证据失败", "rule_reject": "规则拦下", "review": "审核", "reject": "拒绝",
                 "pending": "等你批准", "defer": "等速度上限", "apply": "生效", "canon_seen": "记下正典",
                 "canon_change": "正典变了", "canon_pause": "与正典冲突，暂停", "canon_maybe": "可能与正典冲突",
                 "maintenance": "每日维护", "protect": "保护", "unprotect": "取消保护", "freeze": "冻结",
                 "unfreeze": "解冻", "rollback": "撤销", "restore": "恢复", "delete": "删除",
                 "clear_shadow": "清空影子结果", "mode": "切换模式"}
        out = []
        for a in reversed(self.rt.persona.data["audit"][-n:]):
            t = time.strftime("%m-%d %H:%M", time.localtime(a["time"]))
            rest = {k: v for k, v in a.items() if k not in ("time", "event", "phase")}
            out.append(f"{t} {'〔影子〕' if a.get('phase') == 'shadow' else ''}{names.get(a['event'], a['event'])}  "
                       + json.dumps(rest, ensure_ascii=False))
        return "审计日志（最新在上）：\n" + ("\n".join(out) or "（还没有记录）")

    def _persona_detail(self, which: str):
        from .persona import CAND_CN, OP_CN, STATUS_CN

        p = self.rt.persona
        lst, other = ((self.persona_entries, self.persona_pool) if which == "entry"
                      else (self.persona_pool, self.persona_entries))
        it = lst.currentItem()
        if it is None:
            return
        other.blockSignals(True)
        other.setCurrentItem(None)
        other.blockSignals(False)
        key = it.data(Qt.UserRole)
        lines = []

        def ev_lines(evs):
            for e in evs:
                who = "你" if e["role"] == "user" else "她"
                lines.append(f"  {e['date']} {who}（{'强' if e['trust'] == 'strong' else '弱'}证据）：「{e['quote']}」"
                             + (f"  ← 前一句：{e['prev'][:40]}" if e.get("prev") else ""))

        if which == "entry":
            e = p.entry(key)
            if not e:
                return
            lines.append(f"{e['id']}  {e['text']}")
            lines.append(f"状态：{STATUS_CN.get(e['status'], e['status'])}；强度 {e['strength']:.2f}；类别 {e['kind']}"
                         + ("；🔒 受保护" if e.get("protected") else "")
                         + (f"；{e.get('retire_reason', '')}" if e["status"] == "retired" else "")
                         + (f"；{e.get('pause_reason', '')}" if e["status"] == "paused" else "")
                         + (f"；{e['canon_note']}" if e.get("canon_note") else ""))
            lines.append("强度曲线：" + " → ".join(f"{h['strength']:.2f}" for h in e.get("history", [])))
            lines.append("变化历史：")
            for h in e.get("history", []):
                t = time.strftime("%m-%d %H:%M", time.localtime(h["time"]))
                lines.append(f"  {t} {OP_CN.get(h['op'], h['op'])}（{'你' if h['by'] == 'user' else '管线'}）"
                             f"{h.get('note', '')}  {h['text']}  [{STATUS_CN.get(h['status'], h['status'])} {h['strength']:.2f}]")
            lines.append("证据：")
            ev_lines(e.get("evidence", []))
        else:
            c = p.candidate(key)
            if not c:
                return
            lines.append(f"{c['id']}  {OP_CN.get(c['op'], c['op'])}  {c.get('text', '')}  目标 {c.get('target', '—')}"
                         + (f" → {c['into']}" if c.get("into") else ""))
            lines.append(f"状态：{CAND_CN.get(c['status'], c['status'])}  {c.get('why', '')}")
            lines.append(f"门槛：{p.ready(c)[1]}")
            lines.append(f"她的理由：{c.get('reason', '')}")
            rv = c.get("review")
            if rv:
                lines.append(f"审核：证据支持 {rv['supported']}，过度推断 {rv['overreach']}，与正典冲突 {rv['canon_conflict']}，"
                             f"像注入 {rv['injection']}，涉及价值观 {rv['values']}，有反例 {rv['counter_found']}，"
                             f"风险 {rv['risk']}，把握 {rv['confidence']:.2f}。{rv.get('note', '')}")
            lines.append("证据（程序按编号取回的原文）：")
            ev_lines(c.get("evidence", []))
            if c.get("dropped"):
                lines.append("作废的引用：" + "；".join(f"{d['ref']}「{d['quote'][:20]}」{d['why']}" for d in c["dropped"]))
        self.persona_detail.setPlainText("\n".join(lines))

    @staticmethod
    def _persona_sel(lst):
        it = lst.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _persona_protect(self):
        pid = self._persona_sel(self.persona_entries)
        e = self.rt.persona.entry(pid) if pid else None
        if e:
            self.rt.persona.user_protect(pid, not e.get("protected"))
            self._refresh_persona()

    def _persona_rollback(self):
        pid = self._persona_sel(self.persona_entries)
        if pid and self.rt.persona.user_rollback(pid):
            self._refresh_persona()

    def _persona_restore(self):
        pid = self._persona_sel(self.persona_entries)
        if pid and self.rt.persona.user_restore(pid):
            self._refresh_persona()

    def _persona_delete(self):
        from PySide6.QtWidgets import QMessageBox

        pid = self._persona_sel(self.persona_entries)
        if pid and QMessageBox.question(self, "删除条目", f"永久删除 {pid}？（撤销和恢复都找不回来）") == QMessageBox.Yes:
            self.rt.persona.user_delete(pid)
            self._refresh_persona()

    def _persona_approve(self):
        cid = self._persona_sel(self.persona_pool)
        c = self.rt.persona.candidate(cid) if cid else None
        if not c or c["status"] != "pending_approval":
            self.persona_detail.setPlainText("只有“等你批准”的候选可以同意。")
            return
        pid = self.rt.persona.user_approve(cid, self.rt.cfg)
        self._refresh_persona()
        self.persona_detail.setPlainText(f"已生效：{pid}" if pid else "批准时复查不通过，已拒绝（见审计日志）")

    def _persona_reject(self):
        cid = self._persona_sel(self.persona_pool)
        if cid and self.rt.persona.user_reject(cid):
            self._refresh_persona()

    def _persona_reflect(self):
        started = self.rt.reflect_now(force=True)
        self._refresh_persona()
        self.persona_detail.setPlainText("夜间反思已在后台开始（只读桌宠的记忆库，不写）……" if started
                                         else f"没有开始：{(self.rt.last_reflect or {}).get('error', '')}")

    def _persona_clear(self):
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "清空影子结果",
                                "清空影子模式期间形成的条目和候选池？回顾进度和审计日志会保留。") == QMessageBox.Yes:
            n = self.rt.persona.user_clear_shadow()
            self._refresh_persona()
            self.persona_detail.setPlainText(f"已清空 {n} 条影子条目。")

    def _persona_freeze(self, on: bool):
        if bool(on) != self.rt.persona.frozen:
            self.rt.persona.user_set_frozen(on)
            self._refresh_persona()

    def _persona_set_mode(self, _index: int):
        from PySide6.QtWidgets import QMessageBox

        from .persona import MODE_CN, MODES

        cfg = self.rt.cfg
        new, old = self.persona_mode.currentData(), cfg.persona_mode
        if new == old:
            return
        if new in ("trial", "live") and old not in ("trial", "live"):
            n = sum(1 for e in self.rt.persona.live_entries() if e.get("phase") == "shadow")
            box = QMessageBox(self)
            box.setWindowTitle("开启副人格")
            box.setText(f"切换到「{MODE_CN[new]}」后，副人格条目会进入她的提示词（排在人设之后，优先级更低）。"
                        + (f"\n影子模式期间形成了 {n} 条条目，要保留它们吗？" if n else ""))
            keep = box.addButton("保留并开启" if n else "开启", QMessageBox.AcceptRole)
            clear = box.addButton("清空后开启", QMessageBox.DestructiveRole) if n else None
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() not in (keep, clear) or box.clickedButton() is None:
                self.persona_mode.setCurrentIndex(MODES.index(old) if old in MODES else 0)
                return
            if clear is not None and box.clickedButton() is clear:
                self.rt.persona.user_clear_shadow()
        cfg.persona_mode = new
        cfg.save()
        self.rt.persona.audit("mode", by="user", old=old, new=new)
        self.rt.persona.save()
        self._refresh_persona()

    def _build_display(self):
        w = QWidget()
        f = QFormLayout(w)
        cfg = self.rt.cfg
        bg = QComboBox()
        modes = ["none", "log", "code", "rain", "noise"]
        bg.addItems(modes)
        bg.setCurrentText(self.rt.engine.s.background)
        bg.currentTextChanged.connect(lambda m: (self.rt.engine.set_background(m), setattr(cfg, "background", m), cfg.save()))
        tex = QCheckBox("身体字符流纹理（MV 风格）")
        tex.setChecked(self.rt.engine.s.code_rows)
        tex.toggled.connect(self._tex)
        cell = QSpinBox()
        cell.setRange(5, 24)
        cell.setValue(cfg.cell_h)
        cell.valueChanged.connect(lambda h: self.view.set_cell_height(h))
        plate = QComboBox()
        plate.addItems(["tint", "solid", "none"])
        plate.setCurrentText(cfg.plate_mode)
        plate.currentTextChanged.connect(lambda m: (self.view.set_style(plate_mode=m), self.rt.engine.tick(0)))
        glow = QSlider(Qt.Horizontal)
        glow.setRange(0, 100)
        glow.setValue(int(cfg.glow * 100))
        glow.valueChanged.connect(lambda v: self.view.set_style(glow=v / 100))
        auto = QCheckBox("自主表达（空闲时偶尔自己调用 API 做小动作）")
        auto.setChecked(cfg.autonomous)
        auto.toggled.connect(lambda on: (setattr(cfg, "autonomous", on), cfg.save()))
        attach = QCheckBox("随桌宠启动字符画窗口，并与对话联动")
        attach.setChecked(cfg.attach_to_pet)
        attach.toggled.connect(lambda on: (setattr(cfg, "attach_to_pet", on), cfg.save()))
        hide_pet = QCheckBox("随桌宠启动时隐藏原来那个会到处跑的桌宠（重启生效）")
        hide_pet.setChecked(cfg.hide_original_pet)
        hide_pet.toggled.connect(lambda on: (setattr(cfg, "hide_original_pet", on), cfg.save()))
        pet_menu = QCheckBox("在字符画窗口上右键时，弹出原桌宠的完整菜单")
        pet_menu.setChecked(cfg.integrate_pet_menu)
        pet_menu.toggled.connect(lambda on: (setattr(cfg, "integrate_pet_menu", on), cfg.save()))
        link = QComboBox()
        link.addItems(["inline", "ai", "local", "off"])
        link.setCurrentText(cfg.chat_link)
        link.currentTextChanged.connect(lambda m: (setattr(cfg, "chat_link", m), cfg.save()))
        pet = QPushButton("打开透明桌宠窗口")
        pet.clicked.connect(self.open_pet)
        f.addRow("背景", bg)
        f.addRow(tex)
        f.addRow("字符高度(px)", cell)
        f.addRow("衬底", plate)
        f.addRow("外发光", glow)
        f.addRow(auto)
        f.addRow(attach)
        f.addRow(hide_pet)
        island = QCheckBox("灵动岛收进字符画窗口右下角的按钮（点击展开）")
        island.setChecked(cfg.integrate_island)
        island.toggled.connect(self._toggle_island)
        f.addRow(island)
        f.addRow(pet_menu)
        from .chat_theme import SKIN_LABELS

        chat_skin = QComboBox()
        for key, label in SKIN_LABELS:
            chat_skin.addItem(label, key)
        keys = [k for k, _ in SKIN_LABELS]
        chat_skin.setCurrentIndex(keys.index(cfg.chat_theme) if cfg.chat_theme in keys else keys.index("ascii"))
        chat_skin.currentIndexChanged.connect(lambda i, box=chat_skin: self._set_chat_skin(box.itemData(i)))
        chat_skin.setToolTip("经典“AI 对话”窗口的样子；随桌宠运行时立即生效")
        f.addRow("“AI 对话”窗口风格", chat_skin)
        f.addRow("对话联动方式", link)
        f.addRow(pet)
        info = QLabel(f"配置文件：{cfg.path}\n数据目录：{data_dir()}")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(info)
        self.tabs.addTab(w, "显示与设置")

    def _tex(self, on):
        self.rt.engine.s.code_rows = on
        self.rt.engine._body_dirty = True
        self.rt.cfg.code_rows = on
        self.rt.cfg.save()

    def open_pet(self):
        from .window import AsciiPetWindow

        if self.pet_window is None:
            self.pet_window = AsciiPetWindow(self.rt, open_lab=self.show, on_close=self._pet_closed)
        self.pet_window.show()

    def _pet_closed(self):
        self.pet_window = None

    def showEvent(self, e):
        if self.view not in self.rt.views:            # 关掉后再打开：重新接上画面
            self.rt.add_view(self.view)
            c, k, w, h, off = self.rt.engine.frame()
            self.view.set_frame(c, k, w, h, off)
        super().showEvent(e)

    def closeEvent(self, e):
        self.rt.remove_view(self.view)
        if self._owns_rt and self.pet_window is None:
            self.rt.stop()
        super().closeEvent(e)


def main(argv=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    lab = Lab()
    lab.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
