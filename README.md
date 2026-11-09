# PRGuard

PRGuard 是面向 Pull Request 的五角色本地代码评审对话系统。本目录是已准备好的本机运行包：Streamlit 应用、Qwen3-1.7B 基座、五个角色 Adapter、测试角色旧 Adapter 回退、角色知识库、相似评论检索语料、Python 3.11 运行时和虚拟环境都放在本目录内。

运行不需要 DashScope 或其他模型 API Key；会话保存在本机 SQLite。应用不会自动批准、合并或向 GitHub 写入评论。模型回复只作为工程师复核的评审建议。

## 启动

在 Windows 命令提示符（CMD）中从项目目录启动：

```cmd
cd /d D:\Projects\S-1-Multi-agent\PRGuard
.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 Streamlit 显示的本机地址（默认 `http://localhost:8501`）。模型在首次发起评审时加载，第一次可能需要等待。项目用 `.env` 读取本目录下的模型、Adapter 和会话数据库路径；`.env.example` 是可移植的默认配置。

需要 NVIDIA GPU 和可运行当前 PyTorch/CUDA 环境的显卡驱动。角色声音由浏览器的 SpeechSynthesis 提供，实际声音取决于当前系统。

## 本地运行文件

- `app.py`、`src/prguard/`：聊天界面、对话记忆、角色编排、检索、模型路由和证据校验。
- `models/base/Qwen3-1.7B/`：本地开源基座模型。
- `outputs/adapters/by-role-selected/`：五个角色的证据感知 Adapter。
- `outputs/adapters/by-role/test_reviewer/`：测试/回归角色的旧版回退 Adapter。
- `data/knowledge/`：五个角色的专业知识库。
- `data/processed/train.jsonl`：运行时相似评论检索语料。
- `assets/avatars/*.svg`：运行时角色头像。
- `.venv/`：已安装的本地 Python 依赖。
- `.runtime/Python311/`：本项目虚拟环境使用的 Python 3.11 基础运行时。
- `outputs/conversations/`：首次使用时创建的 SQLite 对话历史。

训练用原始数据、其他训练检查点和日志，以及课程报告、PPT、开发说明和设计归档在同级 `PRGuard_过程材料` 中；它们不参与应用运行。

## Git 说明

模型、Adapter、虚拟环境、随项目打包的 Python 运行时、原始/处理数据、日志和 SQLite 数据库由 `.gitignore` 排除，所以提交源码仓库时不会上传这些大文件。GitHub 克隆仓库后需要自行安装环境、下载模型和数据并训练/恢复 Adapter；本地这个运行目录已经包含所需文件。运行还需要本机 NVIDIA 驱动以及浏览器/操作系统提供的语音合成能力。
