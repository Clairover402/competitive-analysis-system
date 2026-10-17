# -*- coding: utf-8 -*-
"""内容质控单测 —— normalize_text / readable_char_count / is_boilerplate_chunk。

用例样本全部取自 baseline=runs/20261013-230903 的真实 chunk（1352 条），
覆盖三类边界：
  - 必须判样板：图片墙、目录树、备案页脚、URL 残渣；
  - 必须保留：官方正文（夹大量行内链接）、英文文档、短小正文；
  - 规整不变量：锚文本保留、data URI 清除、幂等性。
"""
from __future__ import annotations

from src.agents.content_quality import (
    clean_chunk_text,
    is_boilerplate_chunk,
    normalize_text,
    readable_char_count,
)

# ─────────────────────────────────────────────────────────────
# §1 normalize_text：标记清洗
# ─────────────────────────────────────────────────────────────


def test_normalize_strips_data_uri_svg():
    raw = "[![ONES](data:image/svg+xml,%3csvg%20viewBox='0%200%2086%2024'%3e%3c/svg%3e)](https://ones.cn)"
    out = normalize_text(raw)
    assert "data:" not in out
    assert "svg" not in out
    assert "https://ones.cn" not in out


def test_normalize_keeps_anchor_text_drops_url():
    raw = '[在表格中设置数据格式](/hc/zh-CN/articles/360033771233 "在表格中设置数据格式")'
    out = normalize_text(raw)
    assert "在表格中设置数据格式" in out
    assert "/hc/zh-CN/articles" not in out
    assert "360033771233" not in out


def test_normalize_drops_markdown_image_entirely():
    raw = "正文A\n\n![](https://p1-hera.feishucdn.com/tos-cn-i-jbbdkfciu3/xx~tplv-image:0:0.image)\n\n正文B"
    out = normalize_text(raw)
    assert "正文A" in out and "正文B" in out
    assert "feishucdn.com" not in out
    assert "![" not in out


def test_normalize_strips_bare_and_relative_urls():
    raw = "参见 https://www.notion.com/help 与 //assets.cnblogs.com/logo.svg 说明"
    out = normalize_text(raw)
    assert "https://" not in out
    assert "//assets." not in out
    assert "参见" in out and "说明" in out


def test_normalize_removes_zero_width_and_collapses_blanks():
    raw = "多维表格\u200b可联动\u200bAI\n\n\n\n生成话术"
    out = normalize_text(raw)
    assert "\u200b" not in out
    assert "\n\n\n" not in out


def test_normalize_is_idempotent_on_clean_text():
    clean = "飞书企业版定价策略深度解析\n\n2024年Q4价格调整为¥180/人/月。"
    assert normalize_text(normalize_text(clean)) == normalize_text(clean)


def test_normalize_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────
# §2 readable_char_count
# ─────────────────────────────────────────────────────────────


def test_readable_counts_cjk_and_latin_only():
    # 数字 / 标点 / 空白 不计
    assert readable_char_count("飞书 Abc 123 ，。 ！") == 2 + 3


def test_readable_zero_for_url_only():
    # 契约：readable_char_count 的入参应为 normalize_text 之后的文本
    assert readable_char_count(normalize_text("https://a.b/c?x=1&y=2")) == 0


# ─────────────────────────────────────────────────────────────
# §3 is_boilerplate_chunk —— 必须判样板（真阳性）
# ─────────────────────────────────────────────────────────────


def test_image_wall_is_boilerplate():
    # 飞书 content/topic/base 的图片墙块
    raw = ("[![](https://p1-hera.feishucdn.com/tos-cn-i-jbbdkfciu3/eff71c9398c04ebd8452d748646fc1e8.png"
           "~tplv-jbbdkfciu3-png:0:0.png)](https://www.feishu.cn)\n\n"
           "[![](https://p1-hera.feishucdn.com/tos-cn-i-jbbdkfciu3/18df0239e059412985b3fbe01a2345c"
           "~tplv-jbbdkfciu3-image:0:0.image)](https://www.toutiao.com/c/user/3073831497904140/)")
    assert is_boilerplate_chunk(raw) is True


def test_icp_footer_is_boilerplate():
    raw = ("Copyright © 2026 北京飞书科技有限公司京ICP备16045432号-4 "
           "京公网安备 11010802029085号增值电信业务经营许可证：京B2-20190249\n\n"
           "联系我们立即试用\n\n预约顾问购买热线联系我们")
    assert is_boilerplate_chunk(raw) is True


def test_url_fragment_residue_is_boilerplate():
    raw = "项目]("
    assert is_boilerplate_chunk(raw) is True


def test_english_nav_menu_is_boilerplate():
    # 语雀英文导航菜单：可读字符 ~72（> 60 逃过 R1），且无中文签名 → 由 R3 捕获
    raw = ("* Homepage\n* Group use\n* Template Gallery\n* Security\n* Welfare\n"
           "* About Us\n* Download\n* Pricing\n* Help\n* Contact")
    assert is_boilerplate_chunk(raw) is True


def test_chinese_nav_menu_is_boilerplate():
    raw = ("关于我们\n联系我们\n加入我们\n帮助中心\n开放平台\n更新日志\n管理后台\n服务协议")
    assert is_boilerplate_chunk(raw) is True


def test_long_bullet_content_not_menu():
    # 真正的项目符号正文：每行 20+ 可读字符 → 不能被 R3 误杀
    raw = ("* 内置 Web 页面预览，方便前端开发者在 IDE 内直接调试和预览网页效果。\n"
           "* 上传设计草图或 UI 稿，Trae 可自动生成对应的 HTML、CSS 或其他前端代码。\n"
           "* 支持多轮对话式修改，逐步逼近目标实现，降低从零手写的成本。\n"
           "* 内置智能补全与代码解释，帮助开发者快速理解陌生代码库。\n"
           "* 支持多种主流编程语言与框架，覆盖前端、后端与脚本开发场景。\n"
           "* 提供实时代码审查建议，在编码阶段提前发现潜在问题。")
    assert is_boilerplate_chunk(raw) is False


def test_api_doc_code_listing_is_boilerplate():
    # 【2026-10-16 新增 R4 的正例】钉钉开发者文档 API 清单的形态
    # （真实样本见 task 33a0856e 的「钉钉@定价[3]」，1958 字）：
    # 整块可读 > 300（R2 进不去）且块内含长句（R3 要求每行都短，不成立）
    # → R1/R2/R3 全部穿透，只有新增的 R4（代码文件名 ≥ 5）能捕获。
    raw = (
        "应用中心  开发者文档  开发者后台  登录  文档中心\n"
        "meeting.py  calendar_free_slot_finder.py  todo_batch_create.py\n"
        "contact_create.py  user_batch_get.py  department_list.py\n"
        "官方文档目录，包含服务端 API 与客户端 JSAPI 的完整索引。\n"
    )
    assert is_boilerplate_chunk(raw) is True


def test_empty_is_boilerplate():
    assert is_boilerplate_chunk("") is True
    assert is_boilerplate_chunk("   \n  ") is True


# ─────────────────────────────────────────────────────────────
# §4 is_boilerplate_chunk —— 必须保留（真阴性 / 防误杀）
# ─────────────────────────────────────────────────────────────


def test_weaver_official_content_kept():
    # 泛微 E10 功能正文：链接密度高（ld≈0.32），但是真正文
    raw = ("进度安排进行设置，支持添加子项目。\n\n项目任务排布\n\n点击项目卡片的【任务列表】，"
           "可新增任务及子任务，根据实际任务完成情况生成甘特图。\n\n"
           "项目任务执行\n\n点击【项目】的【任务执行】，任务的负责人可以单独查看进行中的、"
           "超期的和未开始的任务，后面的数字代表了有多少待执行的任务，了解任务的执行情况。")
    assert is_boilerplate_chunk(raw) is False


def test_feishu_inline_link_content_kept():
    # 飞书多维表格正文：夹大量行内链接（ld≈0.55），但正文可读字符充足
    raw = ("跨平台同步\n\n同一份飞书多维表格数据，可以同时服务于：\n\n* 官网\n\n* 个人网站\n\n"
           "* 微信小程序（[立即试用](https://www.feishu.cn/register?from=SEO)）\n\n* 公众号\n\n"
           "数据源只有一个，管理入口也只有一个。这对小团队来说尤其重要：减少重复维护，"
           "减少数据不一致。")
    assert is_boilerplate_chunk(raw) is False


def test_notion_faq_with_image_kept():
    # Notion 帮助中心 FAQ：块内有一张图（img 占比 0.35），但正文是真实问答
    raw = ("有数据库全部权限的访客可以创建自动化吗？\n\n很遗憾，访客无法创建数据库自动化。\n\n"
           "为什么我的数据库自动化没有触发？\n\n如果你的数据库自动化没有触发，可能是因为：\n\n"
           "该自动化被设置为由另一个自动化触发。目前自动化无法触发其他自动化。"
           "请尝试使用其他非自动化的触发器。")
    assert is_boilerplate_chunk(raw) is False


def test_english_doc_kept():
    # 英文官方文档（CJK 为 0），旧页面级门禁会整页误杀，此处必须保留
    raw = ("Automations can be triggered when a database entry is created, edited, or deleted. "
           "You can also schedule automations to run at a specific time. Note that an automation "
           "cannot trigger another automation to avoid infinite loops.")
    assert is_boilerplate_chunk(raw) is False


def test_short_but_real_content_kept():
    # Trae 功能列表：块短（可读≈93），但不含页脚签名 → 保留
    raw = ("**Webview 功能**\n\n* 内置 Web 页面预览，方便前端开发者在 IDE 内直接调试和预览网页效果。\n\n"
           "**图像转代码**\n\n* 上传设计草图或 UI 稿，Trae 可自动生成对应的 HTML、CSS 或其他前端代码。")
    assert is_boilerplate_chunk(raw) is False


def test_few_code_filenames_in_text_kept():
    # 【2026-10-16 R4 的防误杀反例】正文里顺带提到一两个脚本名/配置名，
    # 命中数（2）远低于 R4 阈值（5）→ 必须保留。
    raw = ("该项目提供了一个命令行工具，入口脚本是 main.py，配置文件为 config.yaml。"
           "运行时它会读取环境变量、连接数据库，并把结果写入 CSV 文件供后续分析使用。")
    assert is_boilerplate_chunk(raw) is False


# ─────────────────────────────────────────────────────────────
# §5 clean_chunk_text 兜底
# ─────────────────────────────────────────────────────────────


def test_clean_chunk_text_falls_back_when_all_markup():
    raw = "![](https://x.com/a.png)"
    # 规整后为空 → 退回原文，不产生空串
    assert clean_chunk_text(raw) == raw


def test_clean_chunk_text_normalizes_otherwise():
    raw = "见 [文档](https://x.com/y) 说明"
    assert clean_chunk_text(raw) == "见 文档 说明"
