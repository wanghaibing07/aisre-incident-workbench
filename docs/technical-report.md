# AI SRE 技术复盘：Kubernetes 故障调查与 Agent 工具扩展

本项目在独立 Kubernetes 实验命名空间中，通过 HolmesGPT 0.43.0 调查两个受控故障，使用固定范围的只读工具获取证据，并由工程师控制恢复服务。

## T1：Service Selector 不匹配

Pod Ready，但 Service Selector 与 Pod 标签不一致，EndpointSlice 无可用后端。一次 Magpie 模型调查正确定位了选择器问题；恢复 Selector 后，HTTP 恢复 200。

[详细记录](t1-investigation.md)

## T3：Nginx 网关上游端口错误

网关健康检查返回 200，但业务 API 返回 502。下游 Service 正常暴露端口 80，注入错误后网关使用端口 81。

| 调查 | 关键观察 | 结果 |
|---|---|---|
| Magpie `workbuddy/glm-5.3-flash`，Windows 桥接 | 日志上游端口 81，Service 端口 80 | 模型指出端口不匹配 |
| Qwen2.5-7B，Linux 原生调查 | 工具返回了同类端口线索 | 模型提到 81，但未与 80 对照，根因判断未通过 |

两次调查分别使用不同模型和环境。恢复网关配置后，两次实验的两个接口均返回 200。

[详细调查](t3-investigation.md) · [自定义工具](../toolsets/incident-lab-readonly.yaml)

## 托管环境复测

在提交 `7d01d46` 的一次 Linux 原生复测中，Qwen2.5-7B 完成工具调用后，T1 和 T3 调查均在 600 秒时超时，未产出最终回答。日志显示模型处理较长输入耗时较高，但不能单凭此次记录确定超时的唯一原因。两次故障均已恢复，`/healthz` 和 `/api/` 返回 200；[复测记录](../evidence/hosted-linux-timeout-failure.json)保留了运行版本和范围。

## 技术取舍与边界

- 复用成熟 Agent，仅补充真正缺少的日志取证能力。
- 工程师控制故障注入和恢复脚本；Agent 无自动恢复功能。
- Agent 没有直接读取 Nginx 配置，判断依据是日志与 Service 信息。
- 这是有限案例的实验观察，不能据此推断生产环境可靠性或统计精度。
- [最小 Kubernetes 资源文件](../k8s/incident-lab.yaml) 已在新建单节点 kind 集群中完成部署、权限及 T1/T3 故障恢复检查，详见[本地验证记录](../evidence/kind-workflow-local.json)。

[架构说明](architecture.md)
