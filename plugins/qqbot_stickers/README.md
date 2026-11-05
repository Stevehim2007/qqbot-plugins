# qqbot_stickers 3.0.0

AstrBot >= 4.29 的独立表情库插件。仅使用 Python 标准库与 AstrBot 公共 API，
不导入 `qqbot_enhancer`、`qqbot_core_full` 或其他插件源码，也没有第三方依赖。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/收藏表情 [分类1,分类2]` | 收藏当前、引用或最近缓存中的图片 |
| `/表情状态` | 只读诊断（别名 `/表情诊断`、`/表情库`） |
| `/表情分类` | 分类与数量 |
| `/表情列表 [分类]` | 列出文件名、分类、尺寸与语义元数据 |
| `/发表情 [分类/情绪/标签]` | 随机发送一张，受冷却与防重复约束 |

在私聊或 @/唤醒场景中，插件还会确定性识别明确的“收藏/保存 / 发个开心表情 /
把这个表情改成开心”等短句。无法确定时保持沉默，不猜测；普通聊天不会产生主回复。
自然语言解析与命令处理不会启动聊天模型。仅在用户显式开启可选语义 AI 分类且
本地确定性解析失败时，才尝试一次有超时、有严格 JSON 边界的分类请求；Provider
不存在、超时、格式错误或置信度不足时自动回退本地逻辑。

## 数据布局

通过 `StarTools.get_data_dir(plugin_name="qqbot_stickers")` 获取每插件数据目录：

```text
<data_dir>/collected_stickers/
├── .write.lock                 # 0600，跨进程 flock
├── index.json                  # 0600，version=1 + schema_version=2
├── 开心/
│   └── <sha256>.png            # 0644
└── 吐槽/
    └── <sha256>.gif
```

`index.json` 只保存规范化元数据，不保存绝对路径：

```json
{
  "version": 1,
  "schema_version": 2,
  "saved_at": 0,
  "stickers": [
    {
      "sha256": "64 lowercase hex chars",
      "filename": "<sha256>.png",
      "category": "开心",
      "categories": ["开心", "日常"],
      "size": 1234,
      "width": 128,
      "height": 128,
      "mime": "image/png",
      "created_at": 0,
      "updated_at": 0,
      "source": "aiocqhttp",
      "tags": ["开心", "happy"],
      "meaning": "表示开心",
      "tone": "expressive"
    }
  ]
}
```

文件实际路径由“规范化分类 + `sha256.ext`”重新推导。索引内 forged path、
目录穿越、符号链接、非普通文件、错误 SHA/文件名会一律丢弃；图片必须通过
文件大小、魔数、宽高和像素数边界检查。

## 备份与恢复

- 备份：停写插件或保持 AstrBot 运行，复制整个
  `collected_stickers/` 目录即可；`index.json` 与分类文件必须一起复制。
- 恢复：先备份目标目录，再将备份目录放回同一 per-plugin 数据目录。
- `index.json` 采用临时文件写入、`fsync`、`os.replace` 和目录 `fsync`。若进程
  在换入索引前崩溃，最坏情况是留下一个未被索引引用的图片文件，不会出现半个
  JSON 索引；后续采集相同 SHA 会安全复用或判重。
- 索引损坏时读取路径会安全退化为空列表，不会删除任何图片。确认内容后可从
  分类目录重建，或重新从旧插件导入。

## 旧 qqbot_enhancer 迁移

插件生命周期首次就绪时，会尝试从
`StarTools.get_data_dir(plugin_name="qqbot_enhancer")/collected_stickers` 导入。
也可以通过 `migrate_legacy_stickers(legacy_root, new_root)` 显式调用。

- 旧目录只读：不删除、不重命名、不改权限、不重写，保留用于回滚。
- 以图片真实字节的 SHA-256 幂等去重；已存在的新条目绝不覆盖。
- 先读取旧 `index.json` 以尽量保留分类/标签/含义；索引 malformed 时安全退回
  目录扫描，跳过无效、超大、非图片、symlink 与越界路径。
- 迁移可重复执行；第二次执行会把已有 SHA 计为 duplicate，而不会新增文件。

## 单写者、并发与权限

所有索引和图片变更都经过两层串行化：

1. 插件事件循环内的 `asyncio.Lock`，避免同一 loop 堆积阻塞写任务；
2. store 的 `fcntl.flock(.write.lock)`，保护跨协程/跨进程的
   “读取索引 → 查重 → 计数 → 写文件 → 原子替换索引”完整序列。

读取索引也持共享 flock。写操作使用 `LOCK_EX`，因此不会读到一半的索引或与另一
写入者交错。安全权限为：

- 目录 `0755`（便于容器内 AstrBot 与宿主机管理用户遍历；不含隐私内容）；
- 图片 `0644`；
- `index.json` 与 `.write.lock` 为 `0600`。

部署在非 POSIX 平台时 `fcntl` 不存在，代码仍可运行，但只有 asyncio 锁提供的
单进程串行化；生产环境应使用 AstrBot 支持的 Linux/POSIX 宿主。

## qqbot_safe_tools 稳定接口

```python
async def tool_send_sticker(self, event, *, category="", emotion="", tag=""): ...
async def tool_collect_sticker(self, event, *, category=""): ...
async def tool_list_categories(self, event): ...
```

三者返回 AstrBot result 或 `None`，任何异常都在插件边界内吞掉并记录，不会向
调用方抛出。过滤参数按 AND 组合；`emotion` 会匹配英文情绪名及其中文关键词。

## core reply 装饰规则

`@filter.on_decorating_result(priority=100)` 只在以下条件全部满足时向结果链追加
**至多一张**图片：

- `qqbot_core_reply_candidate=True`；
- `qqbot_core_reply_allow_sticker=True`；
- 没有 `qqbot_safe_tools_processed`；
- 不是命令结果；
- 结果链中尚无图片；
- 文本中不存在 `[[tool:` / `[[qqbot_tool:` 残留 marker。

插件从不解析、执行、删除或改写工具 marker；不满足条件时原结果完全不动。主回复
owner 始终是 `qqbot_core_reply`。

## 测试

```bash
cd plugins/qqbot_stickers
python3 -m unittest discover -s tests -q
```
