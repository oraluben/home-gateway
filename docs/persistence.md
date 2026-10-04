# 持久化与恢复

## 私有配置

`deployment.json` 包含网络、Hyper-V 参数、VPN profile 名称、pass 项目名称以及本地网关覆盖设置。`vpn/profiles.json` 包含服务端、用户名和凭据引用；交互式 `vpn`/`vpn2` 与网关部署可以共用它。密码继续由现有 pass 项目管理，无需把整个 yadm checkout 交给容器。

如果 yadm 在多台电脑上同步，可将家庭部署文件命名为 `deployment.json##class.home-gateway`，只在家庭操作机设置 `yadm config --add local.class home-gateway` 后执行 `yadm alt`。VPN profiles 可以在公司电脑上通用。加密凭据按现有 `.password-store/.gpg-id` 管理收件人；更换设备需要相应 GPG 私钥。

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

在同一 VM 或重新准备的 VM 上恢复：

```sh
python3 tools/deploy.py --restore /path/to/gateway-backup.tar.gz.gpg --validate-only
python3 tools/deploy.py --restore /path/to/gateway-backup.tar.gz.gpg
```

Windows 使用 `./Deploy-Gateway.ps1 -Restore <文件> -ValidateOnly` 预检，去掉 `-ValidateOnly` 应用。恢复仍以当前 yadm/pass 配置为准，备份中的配置副本用于核对历史；只应用缓存和节点选择。镜像及配置验证成功后才停止服务，已有部署的代码、配置与 UI 保存在 VM 的 `data/backups/deploy-*`，恢复检查失败则回退。

全新 VM 先通过 `Prepare-VM.ps1` 与 `Initialize-VM.ps1` 准备。核验新 VM 的 SSH 主机密钥后更新 known_hosts。基础镜像下载、Docker 软件源和首次构建仍需要网络；可以在维护前保存本机镜像：

```sh
ssh <VM> 'sudo docker save home-gateway:0.2.0 | gzip' > home-gateway-0.2.0.tar.gz
ssh <VM> 'sudo docker load' < home-gateway-0.2.0.tar.gz
```

VM VHDX 和镜像归档是独立的恢复材料，不能提交公开 Git。对于当前已经运行的 VM，无需为了目录整齐移动在线磁盘；记录磁盘实际位置，后续重建使用操作机 `~/.local/state/home-gateway/vm`。

配置部署和恢复会复用已部署、镜像 ID 与构建输入都匹配的镜像，避免每次维护都依赖镜像仓库网络。更新系统包时显式增加 `--rebuild`（Windows 为 `-Rebuild`）；组件版本变动需更新 `versions.json`。

## 故障检查

- 先检查 `Gateway.ps1 status`，确认 DNS、VPN、透明代理和订阅最后成功时间。
- VPN 失败看 `logs-vpn`，修复账号/线路后 `retry-vpn`；不会不停重新登录。
- 代理失败看 `logs-clash`，修复后 `retry-clash`。
- 订阅失败看 `logs-subscription`；上次有效配置继续运行，不需要重启 VPN。
- VM 完全无法启动：把下游路由器 WAN 的网关和 DNS 改回上级网关，先恢复普通上网，再修复 VM。

`Restore-HostNetwork.ps1` 用于创建 Hyper-V 外部交换机失败后的 Windows 网络恢复，不是下游路由器故障接管工具。
