#!/usr/bin/env python3
"""
PRM - 个人关系管理与日程调度工具 (Personal Relationship Manager)

一个基于终端的 CLI 工具，用于追踪联系人、根据等级与生活模式自动计算联系周期，
并提供清晰的日程视图。数据通过 JSON 文件本地持久化。
"""

import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
# 常量与配置定义
# ═══════════════════════════════════════════════════════════════

# 各等级的基础联系周期（天）
TIER_BASE_DAYS: Dict[str, int] = {
    "S": 0,   # S 级不参与调度
    "A": 7,
    "B": 14,
    "C": 30,
    "D": 60,
}

# 模式乘数：{ 模式名: { 标签名: 乘数 } }
# 新增模式只需在此字典中添加一行，无需修改任何逻辑代码
MODE_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    "internship": {"实验室": 2.0},
    "campus":     {"实验室": 0.5},
}

# 反馈质量对 dynamic_multiplier 的调整系数
FEEDBACK_ADJUSTMENTS: Dict[str, float] = {
    "1": -0.10,  # 聊得很好 → 缩短周期
    "2":  0.00,  # 一般 → 无变化
    "3":  0.10,  # 没话找话 → 拉长周期
}

# 数据文件默认路径（与脚本同目录）
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CONTACTS_FILE = os.path.join(DATA_DIR, "prm_contacts.json")
CONFIG_FILE = os.path.join(DATA_DIR, "prm_config.json")


# ═══════════════════════════════════════════════════════════════
# Contact - 联系人数据模型
# ═══════════════════════════════════════════════════════════════

class Contact:
    """表示一个联系人，包含所有业务字段以及序列化方法。"""

    def __init__(self, name: str, tier: str, tags: List[str],
                 last_contact_date: str,
                 dynamic_multiplier: float = 1.0,
                 notes: Optional[List[str]] = None,
                 location: str = "本地",
                 contact_types: Optional[List[str]] = None,
                 topics: Optional[List[str]] = None,
                 interests: Optional[List[str]] = None):
        self.name = name
        self.tier = tier.upper()
        self.tags = tags
        self.last_contact_date = last_contact_date  # "YYYY-MM-DD"
        self.dynamic_multiplier = dynamic_multiplier
        self.notes: List[str] = notes or []
        self.location = location  # "本地" 或其他城市
        self.contact_types = contact_types or ["线上", "线下"]  # 可选联系方式
        self.topics: List[str] = topics or []  # 历史话题
        self.interests: List[str] = interests or []  # 兴趣爱好

    @property
    def last_contact(self) -> date:
        return datetime.strptime(self.last_contact_date, "%Y-%m-%d").date()

    @last_contact.setter
    def last_contact(self, value: date):
        self.last_contact_date = value.strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier": self.tier,
            "tags": self.tags,
            "last_contact_date": self.last_contact_date,
            "dynamic_multiplier": round(self.dynamic_multiplier, 4),
            "notes": self.notes,
            "location": self.location,
            "contact_types": self.contact_types,
            "topics": self.topics,
            "interests": self.interests,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Contact":
        return cls(
            name=data["name"],
            tier=data["tier"],
            tags=data.get("tags", []),
            last_contact_date=data["last_contact_date"],
            dynamic_multiplier=data.get("dynamic_multiplier", 1.0),
            notes=data.get("notes", []),
            location=data.get("location", "本地"),
            contact_types=data.get("contact_types", ["线上", "线下"]),
            topics=data.get("topics", []),
            interests=data.get("interests", []),
        )

    def __repr__(self):
        return f"Contact({self.name}, {self.tier}级)"


# ═══════════════════════════════════════════════════════════════
# Config - 全局运行时配置
# ═══════════════════════════════════════════════════════════════

class Config:
    """管理全局运行状态：当前模式、休眠状态等。"""

    def __init__(self, current_mode: str = "campus",
                 is_paused: bool = False,
                 pause_start_date: Optional[str] = None,
                 custom_tier_days: Optional[Dict[str, int]] = None,
                 local_city: str = "本地",
                 online_interval_multiplier: float = 0.7):
        self.current_mode = current_mode
        self.is_paused = is_paused
        self.pause_start_date = pause_start_date  # "YYYY-MM-DD" or None
        self.custom_tier_days = custom_tier_days or {}
        self.local_city = local_city  # 用户所在城市
        self.online_interval_multiplier = online_interval_multiplier  # 线上联系间隔倍数

    def to_dict(self) -> dict:
        d = {
            "current_mode": self.current_mode,
            "is_paused": self.is_paused,
            "pause_start_date": self.pause_start_date,
            "local_city": self.local_city,
            "online_interval_multiplier": self.online_interval_multiplier,
        }
        if self.custom_tier_days:
            d["custom_tier_days"] = self.custom_tier_days
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        return cls(
            current_mode=data.get("current_mode", "campus"),
            is_paused=data.get("is_paused", False),
            pause_start_date=data.get("pause_start_date"),
            custom_tier_days=data.get("custom_tier_days"),
            local_city=data.get("local_city", "本地"),
            online_interval_multiplier=data.get("online_interval_multiplier", 0.7),
        )


# ═══════════════════════════════════════════════════════════════
# DataStore - JSON 文件持久化层
# ═══════════════════════════════════════════════════════════════

class DataStore:
    """负责联系人列表和配置的 JSON 文件读写。"""

    def __init__(self, contacts_path: str = CONTACTS_FILE,
                 config_path: str = CONFIG_FILE):
        self.contacts_path = contacts_path
        self.config_path = config_path

    # --- 联系人 ---

    def load_contacts(self) -> List[Contact]:
        if not os.path.exists(self.contacts_path):
            return []
        with open(self.contacts_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [Contact.from_dict(item) for item in raw]

    def save_contacts(self, contacts: List[Contact]):
        with open(self.contacts_path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in contacts], f,
                      ensure_ascii=False, indent=2)

    # --- 配置 ---

    def load_config(self) -> Config:
        if not os.path.exists(self.config_path):
            return Config()
        with open(self.config_path, "r", encoding="utf-8") as f:
            return Config.from_dict(json.load(f))

    def save_config(self, config: Config):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# ScheduleEngine - 调度与状态判定引擎
# ═══════════════════════════════════════════════════════════════

class ScheduleEngine:
    """
    核心调度引擎，负责：
    1. 计算下次联系日期
    2. 计算模式乘数
    3. 对联系人列表进行状态分类
    """

    @staticmethod
    def get_mode_multiplier(contact: Contact, mode: str) -> float:
        """根据当前模式和联系人标签，查表获取模式乘数。"""
        tag_multipliers = MODE_MULTIPLIERS.get(mode, {})
        matched = [tag_multipliers[tag] for tag in contact.tags
                   if tag in tag_multipliers]
        # 多个标签匹配时取最小值（最紧迫）；无匹配则为 1.0
        return min(matched) if matched else 1.0

    @staticmethod
    def next_contact_date(contact: Contact, mode: str, config: Config = None) -> Optional[date]:
        """计算联系人的下次应联系日期。S 级返回 None。"""
        base_days = TIER_BASE_DAYS.get(contact.tier)
        if base_days is None or contact.tier == "S":
            return None
        mode_mult = ScheduleEngine.get_mode_multiplier(contact, mode)

        # 根据地点和联系类型调整间隔
        location_mult = 1.0
        if config and contact.location != config.local_city:
            # 不在本地，只能线上联系，使用线上间隔倍数
            location_mult = config.online_interval_multiplier

        effective_days = base_days * mode_mult * contact.dynamic_multiplier * location_mult
        effective_days = max(1, math.ceil(effective_days))
        return contact.last_contact + timedelta(days=effective_days)

    @staticmethod
    def classify(contacts: List[Contact], mode: str, config: Config = None, today: Optional[date] = None
                 ) -> Dict[str, List[Tuple[Contact, Optional[date], int]]]:
        """
        将联系人分为四个类别，返回字典：
          "s_tier":  [(contact, None, 0), ...]
          "overdue": [(contact, next_date, overdue_days), ...]  按逾期天数降序
          "today":   [(contact, next_date, 0), ...]
          "week":    [(contact, next_date, days_left), ...]    按日期升序
        """
        if today is None:
            today = date.today()

        result: Dict[str, list] = {
            "s_tier": [], "overdue": [], "today": [], "week": []
        }

        for c in contacts:
            if c.tier == "S":
                result["s_tier"].append((c, None, 0))
                continue

            nxt = ScheduleEngine.next_contact_date(c, mode, config)
            if nxt is None:
                continue

            delta = (nxt - today).days
            if delta < 0:
                result["overdue"].append((c, nxt, abs(delta)))
            elif delta == 0:
                result["today"].append((c, nxt, 0))
            elif delta <= 7:
                result["week"].append((c, nxt, delta))

        # 逾期按天数降序，本周按剩余天数升序
        result["overdue"].sort(key=lambda x: x[2], reverse=True)
        result["week"].sort(key=lambda x: x[2])
        return result


# ═══════════════════════════════════════════════════════════════
# CLIView - 终端视图渲染
# ═══════════════════════════════════════════════════════════════

class CLIView:
    """负责在终端中输出格式化的日程视图和交互提示。"""

    DIVIDER = "=" * 48

    @staticmethod
    def render_dashboard(classified: dict, config: Config):
        """渲染完整的日程面板。"""
        mode_label = config.current_mode
        pause_label = "  ** 系统已休眠 **" if config.is_paused else ""

        print(f"\n{CLIView.DIVIDER}")
        print(f"   PRM - 个人关系管理  [模式: {mode_label}]{pause_label}")
        print(CLIView.DIVIDER)

        # S-Tier
        CLIView._section("special", "特别关注 S-Tier", classified["s_tier"], config)
        # 逾期
        CLIView._section("overdue", "逾期未联系", classified["overdue"], config)
        # 今日
        CLIView._section("today", "今日必须联系", classified["today"], config)
        # 本周
        CLIView._section("week", "本周即将到来", classified["week"], config)

        print()

    @staticmethod
    def _section(kind: str, title: str, items: list, config: Config = None):
        icons = {
            "special": "*",
            "overdue": "!",
            "today":   ">>",
            "week":    "-",
        }
        icon = icons.get(kind, "-")
        print(f"\n{icon} 【{title}】")
        if not items:
            print("    (无)")
            return

        for contact, nxt, value in items:
            # 确定联系方式提示
            contact_hint = ""
            if config and contact.location != config.local_city:
                contact_hint = " [线上]"
            elif "线下" in contact.contact_types:
                contact_hint = " [线下]"

            if kind == "special":
                print(f"    . {contact.name}{contact_hint}")
            elif kind == "overdue":
                last = contact.last_contact.strftime("%m-%d")
                print(f"    . {contact.name} ({contact.tier}级){contact_hint}  "
                      f"逾期 {value} 天  上次: {last}")
            elif kind == "today":
                note = contact.notes[-1] if contact.notes else "(无备注)"
                topics_hint = f"  话题: {', '.join(contact.topics[-3:])}" if contact.topics else ""
                print(f"    . {contact.name} ({contact.tier}级){contact_hint}  "
                      f"最近备注: \"{note}\"{topics_hint}")
            elif kind == "week":
                nxt_str = nxt.strftime("%m-%d") if nxt else "?"
                print(f"    . {contact.name} ({contact.tier}级){contact_hint}  "
                      f"还剩 {value} 天  预计: {nxt_str}")

    @staticmethod
    def print_help():
        print("""
可用命令:
  view     - 查看本周日程视图
  add      - 添加新联系人
  delete   - 删除联系人
  edit     - 编辑联系人 (姓名/等级/标签/乘数/地点/话题/兴趣)
  contact  - 记录一次联系
  interval - 调整等级联系间隔天数
  mode     - 切换生活模式 (internship / campus)
  pause    - 切换全局休眠 / 恢复
  city     - 设置本地城市
  list     - 列出所有联系人
  help     - 显示此帮助
  quit     - 退出程序
""")


# ═══════════════════════════════════════════════════════════════
# ContactManager - 业务操作层
# ═══════════════════════════════════════════════════════════════

class ContactManager:
    """封装所有联系人和配置相关的业务操作。"""

    def __init__(self, store: DataStore):
        self.store = store
        self.contacts: List[Contact] = store.load_contacts()
        self.config: Config = store.load_config()
        # 应用自定义等级间隔到全局常量
        for tier, days in self.config.custom_tier_days.items():
            TIER_BASE_DAYS[tier] = days

    def save_all(self):
        self.store.save_contacts(self.contacts)
        self.store.save_config(self.config)

    # --- 添加联系人 ---

    def add_contact(self) -> Optional[Contact]:
        name = input("  姓名: ").strip()
        if not name:
            print("  [取消] 姓名不能为空。")
            return None

        # 检查重名
        if any(c.name == name for c in self.contacts):
            print(f"  [错误] 联系人 \"{name}\" 已存在。")
            return None

        tier = input("  等级 (S/A/B/C/D): ").strip().upper()
        if tier not in TIER_BASE_DAYS:
            print(f"  [错误] 无效等级 \"{tier}\"，请输入 S/A/B/C/D。")
            return None

        tags_raw = input("  标签 (逗号分隔，可留空): ").strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

        location = input(f"  常住地 [{self.config.local_city}]: ").strip() or self.config.local_city

        contact_types_raw = input("  联系方式 (线上/线下，逗号分隔) [线上,线下]: ").strip()
        contact_types = [t.strip() for t in contact_types_raw.split(",") if t.strip()] if contact_types_raw else ["线上", "线下"]

        interests_raw = input("  兴趣爱好 (逗号分隔，可留空): ").strip()
        interests = [i.strip() for i in interests_raw.split(",") if i.strip()] if interests_raw else []

        contact = Contact(
            name=name, tier=tier, tags=tags,
            last_contact_date=date.today().strftime("%Y-%m-%d"),
            location=location,
            contact_types=contact_types,
            interests=interests,
        )
        self.contacts.append(contact)
        self.save_all()
        print(f"  [OK] 已添加 {name} ({tier}级)。")
        return contact

    # --- 记录联系 ---

    def record_contact(self):
        name = input("  联系人姓名: ").strip()
        contact = self._find_contact(name)
        if contact is None:
            return

        # 显示话题推荐
        if contact.topics or contact.interests:
            print(f"  💡 话题推荐:")
            if contact.interests:
                print(f"     兴趣: {', '.join(contact.interests)}")
            if contact.topics:
                print(f"     最近话题: {', '.join(contact.topics[-3:])}")

        print("  反馈质量:")
        print("    1. 聊得很好")
        print("    2. 一般")
        print("    3. 没话找话")
        choice = input("  请选择 (1/2/3): ").strip()
        if choice not in FEEDBACK_ADJUSTMENTS:
            print("  [错误] 无效选择。")
            return

        adj = FEEDBACK_ADJUSTMENTS[choice]
        contact.dynamic_multiplier = max(0.1, contact.dynamic_multiplier + adj)

        note = input("  简短备注 (可回车跳过): ").strip()
        if note:
            contact.notes.append(note)

        topic = input("  聊天话题 (可回车跳过): ").strip()
        if topic:
            contact.topics.append(topic)

        contact.last_contact = date.today()
        self.save_all()

        labels = {"1": "聊得很好", "2": "一般", "3": "没话找话"}
        print(f"  [OK] 已记录与 {contact.name} 的联系 "
              f"(反馈: {labels[choice]}, "
              f"multiplier: {contact.dynamic_multiplier:.2f})。")

    # --- 模式切换 ---

    def switch_mode(self):
        available = list(MODE_MULTIPLIERS.keys())
        print(f"  当前模式: {self.config.current_mode}")
        print(f"  可选模式: {', '.join(available)}")
        new_mode = input("  切换到: ").strip().lower()
        if new_mode not in MODE_MULTIPLIERS:
            print(f"  [错误] 未知模式 \"{new_mode}\"。")
            return
        if new_mode == self.config.current_mode:
            print("  [提示] 已处于该模式。")
            return
        self.config.current_mode = new_mode
        self.save_all()
        print(f"  [OK] 模式已切换为 \"{new_mode}\"。")

    # --- 设置本地城市 ---

    def set_local_city(self):
        current = self.config.local_city
        print(f"  当前本地城市: {current}")
        new_city = input("  新城市名称 (回车取消): ").strip()
        if not new_city:
            return
        self.config.local_city = new_city
        self.save_all()
        print(f"  [OK] 本地城市已设为 \"{new_city}\"。")

    # --- 休眠/恢复 ---

    def toggle_pause(self):
        if not self.config.is_paused:
            # 进入休眠
            self.config.is_paused = True
            self.config.pause_start_date = date.today().strftime("%Y-%m-%d")
            self.save_all()
            print("  [OK] 系统已进入休眠，所有倒计时冻结。")
        else:
            # 恢复：为所有非 S 级联系人的 last_contact_date 加上休眠天数
            pause_start = datetime.strptime(
                self.config.pause_start_date, "%Y-%m-%d").date()
            delta = (date.today() - pause_start).days
            if delta > 0:
                for c in self.contacts:
                    if c.tier != "S":
                        c.last_contact = c.last_contact + timedelta(days=delta)
            self.config.is_paused = False
            self.config.pause_start_date = None
            self.save_all()
            print(f"  [OK] 系统已恢复，休眠 {delta} 天，"
                  f"所有联系人日期已顺延。")

    # --- 列出所有联系人 ---

    def list_contacts(self):
        if not self.contacts:
            print("  (暂无联系人)")
            return
        print(f"\n  {'姓名':<12} {'等级':<4} {'常住地':<10} {'联系方式':<12} "
              f"{'上次联系':<12} {'乘数':<6}")
        print("  " + "-" * 68)
        for c in sorted(self.contacts,
                        key=lambda x: list(TIER_BASE_DAYS.keys()).index(x.tier)):
            types = ",".join(c.contact_types) if c.contact_types else "-"
            print(f"  {c.name:<12} {c.tier:<4} {c.location:<10} {types:<12} "
                  f"{c.last_contact_date:<12} {c.dynamic_multiplier:<6.2f}")
        print()

    # --- 删除联系人 ---

    def delete_contact(self):
        name = input("  要删除的联系人姓名: ").strip()
        contact = self._find_contact(name)
        if contact is None:
            return
        confirm = input(f"  确认删除 \"{name}\" ({contact.tier}级)？(y/n): ").strip().lower()
        if confirm != "y":
            print("  [取消] 未删除。")
            return
        self.contacts.remove(contact)
        self.save_all()
        print(f"  [OK] 已删除联系人 \"{name}\"。")

    # --- 编辑联系人 ---

    def edit_contact(self):
        name = input("  要编辑的联系人姓名: ").strip()
        contact = self._find_contact(name)
        if contact is None:
            return

        print(f"  当前信息: {contact.name} | {contact.tier}级 | 常住地: {contact.location}")
        print("  可编辑项 (回车跳过保持不变):")

        new_name = input(f"  新姓名 [{contact.name}]: ").strip()
        if new_name and new_name != contact.name:
            if any(c.name == new_name for c in self.contacts):
                print(f"  [错误] 联系人 \"{new_name}\" 已存在。")
                return
            contact.name = new_name

        new_tier = input(f"  新等级 [{contact.tier}]: ").strip().upper()
        if new_tier:
            if new_tier not in TIER_BASE_DAYS:
                print(f"  [错误] 无效等级 \"{new_tier}\"。")
                return
            contact.tier = new_tier

        new_tags = input(f"  新标签 (逗号分隔) [{', '.join(contact.tags) or '无'}]: ").strip()
        if new_tags:
            contact.tags = [t.strip() for t in new_tags.split(",") if t.strip()]

        new_location = input(f"  新常住地 [{contact.location}]: ").strip()
        if new_location:
            contact.location = new_location

        new_types = input(f"  新联系方式 [{','.join(contact.contact_types)}]: ").strip()
        if new_types:
            contact.contact_types = [t.strip() for t in new_types.split(",") if t.strip()]

        new_interests = input(f"  新兴趣 (逗号分隔) [{', '.join(contact.interests) or '无'}]: ").strip()
        if new_interests:
            contact.interests = [i.strip() for i in new_interests.split(",") if i.strip()]

        new_mult = input(f"  新乘数 [{contact.dynamic_multiplier:.2f}]: ").strip()
        if new_mult:
            try:
                val = float(new_mult)
                if val < 0.1:
                    print("  [错误] 乘数不能小于 0.1。")
                    return
                contact.dynamic_multiplier = val
            except ValueError:
                print("  [错误] 请输入有效数字。")
                return

        self.save_all()
        print(f"  [OK] 联系人已更新: {contact.name} ({contact.tier}级)。")

    # --- 调整等级间隔 ---

    def adjust_intervals(self):
        # 从配置加载自定义间隔（如有）
        custom = getattr(self.config, 'custom_tier_days', None) or {}
        print("  当前各等级联系间隔 (天):")
        for tier in ("S", "A", "B", "C", "D"):
            current = custom.get(tier, TIER_BASE_DAYS[tier])
            default = TIER_BASE_DAYS[tier]
            marker = "" if current == default else " (已自定义)"
            if tier == "S":
                print(f"    {tier}: 不参与调度")
            else:
                print(f"    {tier}: {current} 天{marker}")

        print("  输入要修改的等级 (A/B/C/D)，回车取消:")
        tier = input("  等级: ").strip().upper()
        if not tier:
            return
        if tier not in ("A", "B", "C", "D"):
            print("  [错误] 只能修改 A/B/C/D 的间隔。")
            return

        current = custom.get(tier, TIER_BASE_DAYS[tier])
        new_days = input(f"  {tier} 级新间隔天数 [{current}]: ").strip()
        if not new_days:
            return
        try:
            days = int(new_days)
            if days < 1:
                print("  [错误] 间隔至少为 1 天。")
                return
        except ValueError:
            print("  [错误] 请输入整数。")
            return

        if not hasattr(self.config, 'custom_tier_days') or self.config.custom_tier_days is None:
            self.config.custom_tier_days = {}
        self.config.custom_tier_days[tier] = days
        # 同步到全局常量以便引擎使用
        TIER_BASE_DAYS[tier] = days
        self.save_all()
        print(f"  [OK] {tier} 级间隔已设为 {days} 天。")

    # --- 辅助 ---

    def _find_contact(self, name: str) -> Optional[Contact]:
        for c in self.contacts:
            if c.name == name:
                return c
        print(f"  [错误] 未找到联系人 \"{name}\"。")
        return None


# ═══════════════════════════════════════════════════════════════
# App - 主应用入口
# ═══════════════════════════════════════════════════════════════

class App:
    """交互式命令循环，组合 Manager、Engine、View。"""

    def __init__(self):
        self.store = DataStore()
        self.manager = ContactManager(self.store)
        self._ensure_demo_data()

    def _ensure_demo_data(self):
        """首次运行时写入 5 条演示数据。"""
        if self.manager.contacts:
            return

        demos = [
            Contact("导师王教授", "S", ["实验室"],       "2026-03-01", 1.0,
                    ["学期初见面讨论研究方向"]),
            Contact("张三",     "A", ["实验室", "向上社交"], "2026-03-22", 1.0,
                    ["约了咖啡聊实习经验"]),
            Contact("李四",     "B", ["同学"],          "2026-03-15", 1.0,
                    ["一起复习了操作系统"]),
            Contact("王五",     "C", ["向上社交"],       "2026-03-01", 1.0,
                    ["行业分享会上交流"]),
            Contact("赵六",     "D", ["老朋友"],        "2026-02-01", 1.0,
                    ["过年回家见了一面"]),
        ]
        self.manager.contacts = demos
        self.manager.save_all()
        print("  [初始化] 已加载 5 条演示数据。")

    def show_view(self):
        classified = ScheduleEngine.classify(
            self.manager.contacts, self.manager.config.current_mode, self.manager.config)
        CLIView.render_dashboard(classified, self.manager.config)

    def run(self):
        print("\n  欢迎使用 PRM - 个人关系管理工具！输入 help 查看命令。")
        self.show_view()

        commands = {
            "view":     self.show_view,
            "add":      self.manager.add_contact,
            "delete":   self.manager.delete_contact,
            "edit":     self.manager.edit_contact,
            "contact":  self.manager.record_contact,
            "interval": self.manager.adjust_intervals,
            "mode":     self.manager.switch_mode,
            "pause":    self.manager.toggle_pause,
            "city":     self.manager.set_local_city,
            "list":     self.manager.list_contacts,
            "help":     CLIView.print_help,
        }

        while True:
            try:
                cmd = input("\nPRM> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  再见！")
                break

            if cmd in ("quit", "exit", "q"):
                print("  再见！")
                break
            elif cmd in commands:
                commands[cmd]()
            elif cmd == "":
                continue
            else:
                print(f"  [错误] 未知命令 \"{cmd}\"，输入 help 查看帮助。")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App().run()
