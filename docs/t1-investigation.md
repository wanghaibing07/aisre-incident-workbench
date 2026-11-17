# AI SRE 案例 T1：Service Selector 不匹配

## 故障场景
Kubernetes 实验命名空间：`incident-lab` 的隔离副本

- Deployment：t1-web
- Pod 标签：app=t1-web
- 故障 Service Selector：app=t1-web-fault
- Pod 处于 Ready 状态
- EndpointSlice 没有可用后端
- 通过 Service 请求业务失败

## HolmesGPT 调查

在一次使用 Magpie `workbuddy/glm-5.3-flash` 的调查中，Agent 调用了三个只读工具：

1. incident_lab_pod_snapshot
2. incident_lab_service_route_evidence
3. incident_lab_gateway_error_logs

调查发现：
- Pod 本身正常
- Service Selector 与 Pod 标签不一致
- EndpointSlice 缺少后端

Agent 正确定位 Service 选择器错误。
Agent 调查期间未修改资源。

## 恢复
- 将 Service Selector 恢复为 app=t1-web
- EndpointSlice 恢复可用后端
- HTTP 请求恢复为 200

## 结论
T1 的关键线索来自 Kubernetes 资源状态；本次调查也读取了网关日志。

[模型调查记录](../evidence/holmes-model-investigations.json)包含脱敏后的关键工具证据和回答；[交付脚本复测](../evidence/windows-bridge-current-reproduction.json)也记录了调查和恢复结果。另一次 [Linux 原生调查](../evidence/hosted-linux-real-model-correlated.json)中，Qwen2.5-7B 将不匹配的选择器误判为匹配，未达到根因判断标准。
