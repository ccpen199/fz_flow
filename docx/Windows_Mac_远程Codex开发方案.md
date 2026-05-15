# Windows + Mac 远程 Codex 开发方案
版本：V1.0  
日期：2026-03-08

## 1. 背景与目标

### 1.1 当前问题
- 本机 Mac 同时打开多个项目与多个窗口，内存和空间压力大。
- 需要持续运行编译、索引、测试、Codex，会导致本机发热和卡顿。
- 项目切换成本高，环境容易互相污染。

### 1.2 目标
- 把“代码、依赖、编译、Codex 进程”迁移到 Windows 远程主机。
- Mac 仅作为轻量终端和输入输出界面。
- 支持多项目并行、断线续跑、统一管理。

---

## 2. 方案总览

## 2.1 架构
1. Windows 电脑作为远程开发主机（常开）。
2. Windows 内启用 WSL2 Ubuntu 作为统一 Linux 开发环境。
3. 所有项目代码放在 WSL：`/home/dev/workspace/<project>`。
4. Codex 在 WSL 内运行，使用 `tmux` 管理多个项目会话。
5. Mac 通过 SSH 连接 Windows，再进入 WSL 开发。

## 2.2 预期收益
- Mac 本机资源占用明显降低。
- 多项目环境互不干扰，稳定性更高。
- 断网后会话不丢失（tmux 保活）。
- 未来可平滑迁移到云服务器（命令体系不变）。

---

## 3. 实施步骤（一次性搭建）

## 3.1 Windows：安装 WSL2（PowerShell 管理员）
```powershell
wsl --install -d Ubuntu
```
完成后重启，首次进入 Ubuntu 时创建 Linux 用户（示例：`dev`）。

## 3.2 Windows：启用 SSH 服务（PowerShell 管理员）
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

验证 SSH 服务状态：
```powershell
Get-Service sshd
```

## 3.3 WSL（Ubuntu）：安装基础依赖
```bash
sudo apt update
sudo apt install -y git tmux curl unzip build-essential
mkdir -p ~/workspace
```

## 3.4 WSL：安装 Codex
按你当前已有安装方式在 WSL 中安装，确保 `codex` 命令可执行。  
验证：
```bash
codex --help
```

---

## 4. 网络连接建议

## 4.1 推荐：Tailscale（优先）
优势：
- 不需要公网 IP 和端口映射。
- 连接稳定，安全性更高。
- 在不同网络环境下可直接互连。

做法：
1. Windows 与 Mac 都安装 Tailscale 并登录同一账号。
2. 使用 Tailscale 分配的 IP 进行 SSH 连接。

## 4.2 备选：同局域网直连
- Mac 和 Windows 在同一网段下，使用 Windows 内网 IP SSH。
- 适合家庭或办公室固定网络环境。

---

## 5. Mac 配置

## 5.1 生成 SSH 密钥（若无）
```bash
ssh-keygen -t ed25519 -C "mac-dev"
```

## 5.2 分发公钥到 Windows
方式一（推荐）：
```bash
ssh-copy-id <windows_user>@<windows_ip_or_tailscale_ip>
```

方式二（手动）：
- 将 `~/.ssh/id_ed25519.pub` 内容追加到 Windows 用户的 `authorized_keys`。

## 5.3 配置 `~/.ssh/config`
```sshconfig
Host devwin
  HostName <windows_ip_or_tailscale_ip>
  User <windows_user>
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

---

## 6. 日常使用流程

## 6.1 从 Mac 进入 Windows 并切入 WSL
```bash
ssh -t devwin "wsl -d Ubuntu -u dev"
```

## 6.2 新项目初始化
```bash
cd ~/workspace
git clone <repo-url> proj-a
cd proj-a
tmux new -s proj-a
codex
```

## 6.3 恢复已存在会话
```bash
ssh -t devwin "wsl -d Ubuntu -u dev -- tmux attach -t proj-a"
```

## 6.4 多项目建议
- 一个项目一个 tmux 会话：`proj-a`、`proj-b`、`proj-c`。
- 一个项目一个依赖环境（如 `.venv`、`node_modules`）。
- 同时跑多个项目时不要在同一个会话混用环境变量。

---

## 7. 项目迁移建议

## 7.1 迁移顺序
1. 先迁移 1 个核心项目做试点。
2. 验证编译、测试、Codex 工作流稳定后，再批量迁移。
3. Mac 本地仅保留轻量文档目录或镜像副本。

## 7.2 代码目录规范（WSL）
```txt
/home/dev/workspace/
  project-a/
  project-b/
  project-c/
```

## 7.3 磁盘管理建议
- 定期清理构建缓存、Docker 缓存、旧虚拟环境。
- 大文件（数据集、日志）建议放独立目录并做周期归档。

---

## 8. 故障排查

## 8.1 Mac 无法 SSH 到 Windows
排查项：
1. Windows `sshd` 服务是否运行。
2. 防火墙是否放行 `22/tcp`。
3. IP 是否正确（优先用 Tailscale IP）。
4. 公钥是否正确写入 `authorized_keys`。

测试命令：
```bash
ssh -v devwin
```

## 8.2 连接后会话中断
- 确认 `~/.ssh/config` 已配置 `ServerAliveInterval`。
- 使用 tmux 承载所有长任务，避免断线丢失。

## 8.3 WSL 性能不佳
- 确认代码在 WSL 文件系统中（不要放在 `/mnt/c/...` 进行重度 IO 开发）。
- 关闭不必要的后台进程。
- 扩容 Windows 内存或减少并发任务。

---

## 9. 安全建议

1. SSH 仅使用密钥登录，禁用密码登录（可选强化）。
2. Windows 与 WSL 定期更新补丁。
3. 不将敏感密钥硬编码进仓库，统一使用环境变量或密钥管理。
4. 若对公网开放 SSH，务必配合 Tailscale 或 IP 白名单。

---

## 10. 一键检查清单（上线前）

- [ ] Windows 已安装 WSL2 Ubuntu。  
- [ ] Windows `sshd` 已自动启动。  
- [ ] Mac 可 `ssh devwin` 成功。  
- [ ] WSL 内 `git/tmux/codex` 可执行。  
- [ ] 至少 1 个项目已在 `~/workspace` 跑通。  
- [ ] 能断线后 `tmux attach` 恢复开发会话。  

---

## 11. 后续可扩展

1. 把 Windows 主机升级为云主机（命令体系不变）。  
2. 接入 VS Code Remote-SSH 提升界面体验。  
3. 为每个项目增加容器化环境（Docker Compose）。  
4. 增加自动备份（Git + 定时快照）。  

