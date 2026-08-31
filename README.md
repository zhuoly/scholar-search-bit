# Scholar Search — 学术论文搜索下载 MCP

> **本仓库是 [zhdzh12138/scholar-search](https://github.com/zhdzh12138/scholar-search) 的 fork。** 相对上游做了三处改动：
>
> 1. **修复依赖漂移（与学校无关的真 bug）** — 上游 `requirements.txt` 写的是 `mcp>=1.0.0`，现在会解析到 mcp 2.x；而 2.x 移除了 `@app.list_tools()` / `@app.call_tool()` 装饰器 API，`server.py` 基于 1.x 低层 `Server` 编写，直接 `AttributeError: 'Server' object has no attribute 'list_tools'` 起不来。已锁定 `mcp>=1.9,<2`。
> 2. **学校配置解耦** — 原先硬编码在 `databases/cnki.py` 里的学校名与 IdP 域名抽到了新文件 `carsi_search/school.py`，支持环境变量覆盖。默认值从西安电子科技大学改为**北京理工大学**，换学校只改这一个文件（见「适配学校」一节）。
> 3. **让 `sp_url` 真正生效** — 上游 `registry.py` 中 `sp_url` 的 `{entity_id_raw}` 占位符没有任何代码填充，属于死代码。新增 `get_sp_url()` 填入本校 entityId，未登录时直接给出机构登录直达链接。
>
> IEEE 的搜索 / 详情 / PDF 下载已在北京理工大学账号下端到端实测通过；CNKI 与 ScienceDirect 的配置已就位但未实测。

通过 **CDP 连接用户真实 Chrome/Edge**，一站式搜索和下载 IEEE / ScienceDirect / CNKI 论文。

无需自动化登录 — 用户手动登录一次，cookie 自动保存恢复。

```
chrome/msedge --remote-debugging-port=9222   ← 自动启动（如未运行，Windows 优先 Edge）
       ↓
CDP 连接 (carsi_search/engine.py)      ← cookie 保存/恢复
       ↓
┌──────────┬───────────┬──────────┐
│   IEEE   │Elsevier   │   CNKI   │
│  CARSI   │  CARSI    │   CDP    │
└──────────┴───────────┴──────────┘
```

## 安装

```bash
git clone https://github.com/zhuoly/scholar-search-bit.git
cd scholar-search-bit
pip install -r cnki-ieee-download/requirements.txt
python -m playwright install chromium
```

注册 MCP（全局配置，把 `<克隆目录>` 换成你 clone 仓库的绝对路径）：

```bash
claude mcp add cnki-ieee-download -- python <克隆目录>/cnki-ieee-download/server.py
```

或编辑 `.mcp.json`（参考 `.mcp.json.example`）：

```json
{
  "mcpServers": {
    "cnki-ieee-download": {
      "command": "python",
      "args": ["<克隆目录>/cnki-ieee-download/server.py"]
    }
  }
}
```

## MCP 工具

| 工具 | 说明 |
|------|------|
| `ieee_login` | 连接浏览器，检测 IEEE 登录状态 |
| `ieee_search` | 搜索 IEEE 论文 |
| `ieee_detail` | 获取 IEEE 论文详情 |
| `ieee_download` | 下载 IEEE PDF |
| `sciencedirect_login` | 连接浏览器，检测 ScienceDirect 登录状态 |
| `sciencedirect_search` | 搜索 ScienceDirect 论文 |
| `sciencedirect_detail` | 获取 ScienceDirect 论文详情 |
| `sciencedirect_download` | 下载 ScienceDirect PDF（可能需手动过 Cloudflare） |
| `cnki_search` | 搜索 CNKI；可选 `author`/`journal`/`year_start`/`year_end` 触发专业检索（自动连接 Chrome/Edge） |
| `cnki_login` | 检测 CNKI 登录状态 |
| `cnki_detail` | 获取 CNKI 论文详情 |
| `cnki_download` | 下载 CNKI PDF/CAJ，自动按论文标题重命名 |
| `status` | 显示 CDP 连接状态和各数据库登录状态 |
| `logout` | 断开 CDP（不关闭浏览器） |

## 首次使用

1. 打开 Claude Code
2. 首次调用 MCP 工具时**自动启动 Chrome/Edge**（带 `--remote-debugging-port=9222`，Windows 优先 Edge）
3. 在浏览器窗口中**手动登录**：
   - CNKI：点击"机构登录" → 校外访问 → 选择学校
   - IEEE：点击"Institutional Sign In" → CARSI → 学校认证
   - ScienceDirect：点击"Institutional Sign In" → CARSI → 学校认证
4. Cookie 自动保存 — 后续启动无需重新登录
5. 未登录时 Claude 会提示你在浏览器中登录
6. PDF 下载到调用项目的 `downloads/` 目录

## 功能覆盖

| 功能 | 数据源 | 实现 |
|------|--------|------|
| 英文学术论文搜索 | IEEE Xplore | CDP + CARSI cookie |
| IEEE PDF 下载 | IEEE Xplore | CDP + CARSI cookie + JS fetch |
| 英文学术论文搜索/详情 | ScienceDirect | CDP + CARSI cookie |
| ScienceDirect PDF 下载 | ScienceDirect | CDP + CARSI cookie（Cloudflare 可能需手动验证） |
| 中文学术论文搜索/详情 | CNKI 知网 | CDP 连接真实 Chrome/Edge |
| CNKI PDF/CAJ 下载 | CNKI 知网 | CDP + Browser.setDownloadBehavior（自动按标题重命名） |

## 项目结构

```text
scholar-search/
├── cnki-ieee-download/             # MCP 服务器
│   ├── server.py                   # 入口 + 工具定义 + handler 函数
│   ├── requirements.txt            # 依赖（playwright + mcp）
│   └── carsi_search/               # CDP 引擎 + 数据库适配器
│       ├── engine.py               # CDP 连接 + cookie 持久化
│       ├── registry.py             # 数据库注册表（sp_url 等配置）
│       └── databases/              # ieee / sciencedirect / cnki 适配器
├── downloads/                      # PDF 下载目录
├── .mcp.json.example               # MCP 配置模板
└── README.md
```

## 依赖

| 组件 | 必需 | 用途 |
|------|------|------|
| Claude Code | 是 | MCP 宿主 |
| Chrome / Edge | 是（自动启动） | CDP 连接真实浏览器（Windows 优先 Edge） |
| Playwright + mcp | 是 | MCP 服务器运行时 |
| 机构账号 | 下载需要 | IEEE/ScienceDirect CARSI 认证；CNKI 机构登录 |

## 适配学校（本地修改）

学校相关配置已从代码里抽到 `carsi_search/school.py`，当前默认值为**北京理工大学**：

| 配置 | 默认值 | 用途 | 环境变量覆盖 |
|------|--------|------|--------------|
| `SCHOOL_NAME` | `北京理工大学` | CNKI「校外访问」机构列表里选的学校名 | `CARSI_SCHOOL_NAME` |
| `SCHOOL_IDP_HOST` | `idp.bit.edu.cn` | 判断是否已跳到本校 Shibboleth 认证页 | `CARSI_IDP_HOST` |
| `IEEE_ENTITY_ID` | `https://idp.bit.edu.cn/idp/shibboleth` | IEEE 机构登录 entityId | `CARSI_IEEE_ENTITY_ID` |
| `SD_ENTITY_ID` | 同上 | ScienceDirect 机构登录 entityId | `CARSI_SD_ENTITY_ID` |

换成别的学校时，用 IEEE 官方接口查本校 entityId（浏览器直接打开）：

```
https://ieeexplore.ieee.org/rest/api/auth/wayf-by-displayname?keyword=Beijing%20Institute%20of%20Technology&url=/Xplore/home.jsp
```

返回形如：

```json
[{"name":"Beijing Institute of Technology","entityId":"https://idp.bit.edu.cn/idp/shibboleth",
  "url":"/servlet/wayf.jsp?entityId=https://idp.bit.edu.cn/idp/shibboleth&url=..."}]
```

未登录时 `ieee_login` 会直接给出拼好 entityId 的机构登录直达链接，点开走完本校统一身份认证即可。

## 免责声明

- 原作者在**西安电子科技大学**账号下调试；本副本已适配**北京理工大学**（Shibboleth/CARSI，`idp.bit.edu.cn`），IEEE 搜索/详情/下载实测通过。
- 依赖 `mcp` 锁在 `1.x`：`mcp` 2.x 移除了 `@app.list_tools()` / `@app.call_tool()` 装饰器 API，`server.py` 用的是 1.x 低层 `Server`。
- 其他学校若走 WebVPN 反向代理（域名被改写成 `xxx.webvpn.school.edu.cn`），本项目里硬编码的 `ieeexplore.ieee.org` 地址不适用，需要改 `registry.py` / 适配器里的域名。
- 本项目仅供学术研究使用，请遵守各数据库的使用条款。
- Cookie 文件（`.carsi_state.json`）包含登录凭证，请勿提交到公开仓库。

## 致谢

- [zhdzh12138/scholar-search](https://github.com/zhdzh12138/scholar-search) — 本仓库的上游项目，CDP + CARSI 整体架构出自该项目
- [cnki-skills](https://github.com/cookjohn/cnki-skills) — CNKI 知网 Skills
- [cnki-codex-skills](https://github.com/cfh-7598/cnki-codex-skills) — CDP 连接模式参考

## License

MIT

## Links

**[Linux DO](https://linux.do/)**
