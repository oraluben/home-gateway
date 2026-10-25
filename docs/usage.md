# Mihomo 流量记账

v0.8 默认在镜像内独立运行采集进程，每秒读取一次 `/connections`，计算累计值的差分，按归因合并非零增量；每 5 秒将秒明细和对应检查点在同一个 SQLite 事务中提交。无需在 host 安装采集工具、引入新服务或增加 pass 条目。停止统计不会停止 Clash 或 VPN。

SSH 登录网关后使用：

```sh
gatewayctl usage-status
gatewayctl usage --since 1h --group-by host --proxy-only
gatewayctl usage --since 24h --group-by outbound
gatewayctl usage --since 1h --group-by protocol
gatewayctl usage --since 15m --group-by source
gatewayctl usage --since 5m --group-by time --bucket-seconds 1 --limit 120
gatewayctl usage --since 1h --json
gatewayctl logs-usage
gatewayctl retry-usage
gatewayctl stop-usage
```

`usage-status` 显示最后采样、最后提交、采样耗时、异常标记、累计错误次数及数据库大小；状态不新鲜、进程身份不匹配或采集不正常时返回非零。普通 `gatewayctl status` 同时展示采集状态，但不会因为统计故障把网络判定为不可用。启动时可能需要数秒才能完成首批提交。查询只读取已提交记录，不访问控制接口或解密凭据。

`--since` 支持 `s/m/h/d`，查询返回前 20 项，`--limit` 为 1 到 500；时间分组支持 1、60、3600 秒，按时间倒序。`--proxy-only` 只过滤可归因明细，全局总量仍展示全部 Mihomo 处理流量。JSON 中的字节数是整数，时间是 UTC Unix 毫秒，文本显示使用网关时区。

## 数字含义

- **Global**：Mihomo 全局累计值的差分，包括 DIRECT，可覆盖两次采样之间已结束的短连接。累计值被外部重置、核心重启或未观测到的运行阶段会造成不可恢复的缺口；未知跨界增量不伪装成零。
- **Observed**：采样观察到的连接增量，区分 DIRECT、代理和未知出口。代理链只是标签，不把各级分组重复相加；可识别的 Inner 内部 tracker 不并入业务明细。
- **Difference**：全局减去可归因明细，仅用于核对，不能再加到流量上。两者不是完全原子快照，短时差额可能为负。代理筛选后不计算这个差额。

连接必须在采样时仍存在才能获得归因，短连接和关闭前的尾部字节可能缺失。全局也不等于订阅服务商账单；它与其他直接使用订阅的设备、代理协议开销及计费周期不同。绕过 Mihomo 的公司 VPN 流量不在这里。

归因保存来源 IP、TCP/UDP、域名、目标 IP/端口、入口类型、实际出口及完整链路、命中规则。域名缺失时使用目标 IP，不做额外 DNS 查询，不将 TCP 443 推断成 HTTPS/WSS。上级 Wi-Fi 路由器做 NAT 时只能看到路由器地址，无法识别它后面的具体设备。

## 去重与恢复

首次启用时为已有连接建立基线，不把旧累计值导入当前时间。此后新连接包含第一次观察时已有的字节；连接身份由 Linux 启动身份、核心进程 PID/启动时刻、连接 UUID 和 start 共同确定。

数据库中的累计检查点与流量明细原子提交。采集器重启会继续使用已提交的检查点，未提交的缓存通常约 5 秒；恢复时若连接仍可见，可以补回增量，已经消失的连接无法恢复归因。全局计数单独续算。真正的核心重启开启新的计数身份；只重置全局统计不会清空连接基线。连接计数回退时保留高水位，记录异常，避免重复累计。

连接消失后暂留约一小时检查点；内存最多保留约 5 万条。旧连接在检查点过期后重新出现会先建立基线，宁可把不可确认的部分留作未归因，也不重记旧累计值。

漏采或连接暂时消失后的增量保存实际时间区间，标为 recovered；不能知道它具体发生在哪一秒。按时间查询不把补记放成单秒峰值，补记总量单列。跨越查询起点的明细不强行按比例切分，其完整字节数单列为 boundary bytes，并排除在排行和明细总量之外。

## 持久化与边界

仅四张表：`attribution`、`samples`、`usage`、`checkpoints`。数据位于容器 `/data/usage.sqlite3`，即目标 `/opt/home-gateway/data/usage.sqlite3`；使用 WAL 和 FULL 同步，每 5 秒批量提交。原始 JSON 不落盘，零增量不写明细，节点/域名字符串只存一份。分钟、小时和业务分类在查询时计算，不维护第二套汇总账。

默认保留 48 小时，每分钟小批清理过期数据；长时间停机后的历史清理逐步完成。数据库本体预算默认 1 GiB，WAL 另有少量开销。删除空间优先复用，不周期执行全库压缩。磁盘余量不足 64 MiB、数据库达到容量上限、损坏或事务写入失败时采集进程退出，最多尝试启动三次，耗尽后用 `retry-usage` 手动恢复；不删库重建。HTTP 故障只做退避探测，最多每 30 秒一次，不会重连 VPN 或代理。

可在部署配置的 `runtime` 中覆盖，重新部署生效：

```json
"usage": {"enabled": true, "retention_hours": 48, "max_mib": 1024}
```

关闭时设 `enabled: false`。数据包含访问域名、来源地址和节点名称，属于私有运行数据，不提交公开 Git，也不放进 pass。已有运行状态备份不包含这份数据库；需要导出时应使用 SQLite 备份接口生成一致副本，不单独复制正在写入的主文件。
