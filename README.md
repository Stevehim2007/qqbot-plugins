# qqbot-plugins

独立于 QQ Bot Studio 主仓库的插件清单与发布包。插件默认只使用 Python 标准库，不接受 pip 安装和 post-install 脚本。

## 插件

- `qqbot_web_reader`：`/网页 <url>` 安全读取公开 HTTPS 网页。
- `qqbot_image_search`：`/图片搜索 <关键词>` 搜索并返回图片。

## 发布

每个插件单独打 ZIP，ZIP 唯一顶层目录必须等于插件 ID；清单记录 SHA256、大小和 GitHub Release URL。
