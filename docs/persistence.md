# 持久化与恢复

## 私有配置

完整部署 JSON 默认存于 pass 的 `home-gateway/deployment`，包含目标网络、`host.backend` 与对应参数、VPN profile 名称、凭据引用和网关覆盖设置。共享的 `vpn/profiles` 存放服务端、用户名和密码条目名称。部署工具直接解密这些条目；个人 `vpn`/`vpn2` 辅助脚本也可读取相同 profile，复用原来的密码条目。

pass 支持多行 JSON。初始化用 `pass insert --multiline home-gateway/deployment` 和 `pass insert --multiline vpn/profiles`；修改用 `pass edit <条目>`。配置只针对网关目标；管理端不需要 class 或按操作系统区分的私有文件。[pass 文档](https://www.passwordstore.org/)。

私有 Git/yadm 只需跟踪所需的 `.password-store/**/*.gpg` 与 `.gpg-id`，不跟踪明文部署配置或生成的管理缓存。迁移到另一台管理机可以仅带上这些加密文件和相应 SSH/GPG 密钥，无需同步其余 dotfiles。工具尊重 `PASSWORD_STORE_DIR`；默认使用 `~/.password-store`。收件人列表决定谁能解密，加密文件和私人网络信息仍应留在私有仓库。

成功部署后，工具生成本地 `~/.config/home-gateway/deployment.json`，仅包含地址、SSH/虚拟机参数、接口/控制器地址以及部署 pass 条目的名称。状态、日志、重试等维护动作可用这个缓存，不要求再次解锁 GPG。部署总是重新读取 pass，成功后刷新缓存。Windows 首次使用时自动生成缓存；Linux/WSL 也可运行 `python3 tools/config.py --write-operator ~/.config/home-gateway/deployment.json`。缓存不包含 VPN 服务端/用户名、订阅 URL、密码或 API 密钥，无需手写或提交 Git。

多个网关可以分别保存 pass 条目，用 `--config pass:<条目>` 选择。旧的明文 JSON 文件与 `vpn_profiles_file` 仍兼容，但新部署无需创建它们。Linux SSH 默认读取 `~/.ssh/home-gateway_ed25519` 与 `~/.ssh/home-gateway_known_hosts`；WSL 未配置自身密钥时可复用 Windows 的对应文件。特殊传输路径仍支持显式覆盖，参见 [平台说明](hosts.md)。

部署把最小配置渲染成 `/opt/home-gateway/config/gateway.yaml`，密码放在 `config/secrets/vpn-password`，目录权限为 0700，文件为 0600。容器以只读方式挂载该目录，镜像构建上下文仅允许程序与公开二进制。运行状态写入 `data/`。

密码库更新不会主动改动运行中的 VM；重新部署后生效。订阅则由 VM 每日直接刷新，不需要操作机在线。

## 状态备份

在 WSL/Linux 操作机运行：

```sh
python3 tools/backup.py create
python3 tools/backup.py verify --file ~/.local/state/home-gateway/backups/gateway-YYYYMMDD-HHMMSS.tar.gz.gpg
```

备份默认加密给 password store 的收件人，也可重复传 `--recipient <fingerprint>` 指定。明文只经过进程内存和 SSH，不落在操作机磁盘。归档保存配置现场副本、VPN 密码、有效订阅、地区数据库和当前节点选择；不保存日志、临时策略或正在写入的数据库。

备份文件较大，不必提交 Git；复制到独立磁盘或已有备份服务。SSH 私钥和 GPG 私钥需要独立备份，公开源码与 yadm 无法替代它们。

## 恢复

在同一网关或重新准备的 Ubuntu 目标上恢复；Hyper-V 与 Linux 原生部署使用相同备份格式：

```sh
python3 tools/deploy.py --restore /path/to/gateway-backup.tar.gz.gpg --validate-only
python3 tools/deploy.py --restore /path/to/gateway-backup.tar.gz.gpg
```

Windows 使用 `./Deploy-Gateway.ps1 -Restore <文件> -ValidateOnly` 预检，去掉 `-ValidateOnly` 应用。恢复仍以当前 pass 配置为准，备份中的配置副本用于核对历史；只应用缓存和节点选择。镜像及配置验证成功后才停止服务，已有部署的代码、配置与 UI 保存在目标的 `data/backups/deploy-*`，恢复检查失败则回退。

全新 Hyper-V VM 先通过 `Prepare-VM.ps1` 与 `Initialize-VM.ps1` 准备；Linux 主机先安装系统并配置静态网络。核验目标的 SSH 主机密钥后更新 known_hosts，两种目标都先执行 `tools/prepare-host.py --apply` 再预检和恢复。基础镜像下载、Docker 软件源和首次构建仍需要网络；可以在维护前保存本机镜像：

```sh
ssh <网关> 'sudo docker save home-gateway:0.4.0 | gzip' > home-gateway-0.4.0.tar.gz
ssh <网关> 'sudo docker load' < home-gateway-0.4.0.tar.gz
```

VM VHDX 和镜像归档是独立的恢复材料，不能提交公开 Git。对于当前已经运行的 VM，无需为了目录整齐移动在线磁盘；记录磁盘实际位置，后续重建使用操作机 `~/.local/state/home-gateway/vm`。

配置部署和恢复会复用已部署、镜像 ID 与构建输入都匹配的镜像，避免每次维护都依赖镜像仓库网络。更新系统包时显式增加 `--rebuild`（Windows 为 `-Rebuild`）；组件版本变动需更新 `versions.json`。

预检的镜像使用独立标签，启动配置绑定本次部署的标签。之后的预检或构建不会悄悄改变 VM 下次启动使用的镜像。

## 故障检查

- 先检查 `python3 tools/gateway.py status`（Windows 为 `Gateway.ps1 status`），确认 DNS、VPN、透明代理和订阅最后成功时间。
- VPN 失败看 `logs-vpn`，修复账号/线路后 `retry-vpn`；不会不停重新登录。
- 代理失败看 `logs-clash`，修复后 `retry-clash`。
- 订阅失败看 `logs-subscription`；上次有效配置继续运行，不需要重启 VPN。
- VM 完全无法启动：把下游路由器 WAN 的网关和 DNS 改回上级网关，先恢复普通上网，再修复 VM。

`Restore-HostNetwork.ps1` 用于创建 Hyper-V 外部交换机失败后的 Windows 网络恢复，不是下游路由器故障接管工具。
