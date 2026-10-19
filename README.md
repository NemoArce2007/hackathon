<div align="center">

<h3><img src="static/logo.svg" width="48" alt="句子互动" align="absmiddle" />&nbsp;句子互动</h3>

<img src="docs/title.svg" width="600" alt="企业内部黑客松计分系统" />

**为一场黑客松从开场到颁奖的全部环节而设计的实时计分系统**

飞书一键登录 · 评委与互评双轨计分 · 实时大屏 · 平票决胜

![Python](https://img.shields.io/badge/Python-3.10+-0F172A?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-0F172A?style=flat-square&logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-embedded-0F172A?style=flat-square&logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-0F172A?style=flat-square)

</div>

---

## 概览

一个轻量的 Flask 单体应用，零前端构建，一个 `app.py` 即可上线。参赛者、队长、评委和主持人使用同一套系统，通过飞书 OAuth 完成身份识别，现场所有屏幕实时同步。

## 功能

| 模块 | 说明 |
| --- | --- |
| **飞书登录** | OAuth 授权，按租户限制访问，管理员由 `open_id` 白名单指定 |
| **评委计分** | 管理评委名单并录入票数 |
| **队伍互评** | 自动分配互评对象，队长在 `/peer` 提交 |
| **实时大屏** | `/board` 实时排名，`/leaderboard` 公布榜单 |
| **计时与倒计时** | `/timer` 路演计时，`/countdown` 全场倒计时 |
| **路演顺序** | 一键随机抽签生成 Demo 顺序 |
| **平票决胜** | 自动识别并列队伍，开启多轮 `/tie-vote` 投票 |
| **黑名单与冠军** | 排除违规投票者，后台一键确认冠军 |

## 快速开始

```bash
git clone https://github.com/NemoArce2007/hackathon.git
cd hackathon
pip install -r requirements.txt
cp .env.example .env.local   # 填入飞书应用凭证
python app.py
```

默认运行于 `http://127.0.0.1:5000`。本地调试可设 `ALLOW_DEV_LOGIN=1` 跳过飞书登录，**生产环境务必关闭**。

## 配置

| 变量 | 作用 |
| --- | --- |
| `SECRET_KEY` | Flask 会话密钥 |
| `DATABASE_PATH` | SQLite 文件路径，首次启动自动建表 |
| `PUBLIC_BASE_URL` | 对外访问地址 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证 |
| `FEISHU_REDIRECT_URI` | OAuth 回调，形如 `<base>/auth/feishu/callback` |
| `FEISHU_TENANT_KEY` | 只允许该企业的成员登录 |
| `ADMIN_OPEN_IDS` | 管理员 `open_id`，逗号分隔 |

## 项目结构

```
app.py            应用、路由与计分逻辑
templates/        页面模板（大屏、计时、互评、后台等）
static/logo.svg   品牌标识
```

## 许可证

本项目代码以 [MIT License](LICENSE) 开源。

> **商标声明**：`static/logo.svg` 为句子互动的标识，版权归其所有方，不在 MIT 许可范围内。任何使用均须事先获得相关版权方许可。

<div align="center"><sub>Built by <a href="https://github.com/NemoArce2007">Jimmy Xu</a></sub></div>
