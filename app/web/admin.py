"""内置管理页的资源读取入口。"""

from importlib.resources import files


def render_admin_page() -> str:
    """读取随应用发布的轻量管理页。"""
    return files("app.web").joinpath("admin.html").read_text(encoding="utf-8")
