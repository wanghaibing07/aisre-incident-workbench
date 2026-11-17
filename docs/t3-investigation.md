# AI SRE 案例 T3：Gateway 上游端口故障调查

## 故障场景

网关自身的 `/healthz` 返回 200，但经网关访问的 `/api/` 返回 502。实验中网关使用上游端口 81，而下游 Service 暴露端口 80；Pod 和后端端点正常。

## 调查和关键证据

在 Kubernetes v1.36.4 的隔离实验命名空间中，HolmesGPT 0.43.0 通过三个只读工具查询 Pod、Service 路由和网关日志。一次使用 Magpie `workbuddy/glm-5.3-flash` 的调查给出了以下线索：

- Nginx 错误日志中的上游地址使用端口 **81**。
- Service `t1-web` 暴露端口 **80**，EndpointSlice 有可用后端。
- 模型对照两者后指出端口不匹配，并未直接读取 Nginx 配置。

工程师控制恢复流程，将网关配置恢复为端口 80 并重建 Gateway Pod；`/healthz` 和 `/api/` 均恢复为 200。[模型回答及脱敏关键证据](../evidence/holmes-model-investigations.json)与[交付脚本复测记录](../evidence/windows-bridge-current-reproduction.json)可供查看。

## 不同调查结果

后续另一次在 GitHub 托管 Linux 和 kind 集群中使用 Qwen2.5-7B 的[原生调查](../evidence/hosted-linux-real-model-correlated.json)取得了端口线索，但模型只提到 81，没有与 Service 端口 80 对照，未达到根因判断标准；恢复后两个接口均为 200。

日志工具补充了定位所需的信息，模型能否正确使用仍需结合具体模型与调查结果核验。
