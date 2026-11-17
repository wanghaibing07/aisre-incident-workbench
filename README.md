# AI SRE Incident Investigation Workbench

**让 AI Agent 协助 SRE 排查 Kubernetes 服务故障。** Agent 自动读取工作负载、服务路由和网关日志，分析故障证据、定位错误；工程师根据调查结果执行恢复并验证业务。

基于 HolmesGPT 的 AI SRE 故障调查工作台。重点解决的问题：**Pod 看起来正常，业务接口却失败时，如何从 Kubernetes 资源和日志中找到真正的故障点？**

## 项目故事

业务接口报错时，Pod 仍可能显示 Ready。这个项目把调查拆成可复核的证据：先比较 Pod 标签、Service Selector 和 EndpointSlice，再根据需要读取网关日志。Agent 给出分析，工程师决定是否恢复并检查业务结果。

案例记录中，T1 的 Selector 与 Pod 标签不匹配；T3 则是网关健康检查正常，但代理的上游端口填错。T3 的原工具组合只能缩小范围，增加网关日志工具后，记录显示模型能够对照日志中的 81 和 Service 的 80。这里展示的是**补齐取证工具对定位的帮助**，不能据此声称经过统计验证的提速比例。

面试时可以沿着「故障现象 → 哪个证据缺失 → 如何补工具 → 权限和人工恢复边界 → 怎样复现」讲清楚。历史案例已有调查摘要，原始终端记录未随仓库发布。本轮在 VMware 集群的独立命名空间验证了受限身份、18 项权限拒绝以及 T1/T3 注入和恢复；[结果和范围](evidence/kubernetes-smoke.json)说明了单节点运行的适配。

随后在故障保持期间，使用 HolmesGPT 0.43.0 和本地 Magpie 模型调查，两次均调用三个受限只读工具并指出对应原因；[调查证据](evidence/holmes-model-investigations.json)记录了调用、关键数据和恢复。该次实验通过 Windows 兼容桥接与 SSH 执行取证，没有运行 Linux 原生调查钩子或 GitHub Actions；每种故障只有一次新调查，不能据此推断统计准确率。

后来将 Windows 桥接参数化并补入项目，又在隔离命名空间复测了 T1/T3；[复测证据](evidence/windows-bridge-reproduction.json)记录了脚本哈希、工具和恢复结果。T1 使用交付的等待钩子，T3 使用等价的临时等待钩子；这仍不是统计准确率测试。

远端 CI 首次运行后，复现脚本增加了 504 兼容和失败诊断，两个调查钩子的 T3 故障描述也改为更准确的 5xx。既有实验记录保留当时的脚本哈希，不冒充是在修改后重新执行。

上述改动后，以当前脚本在新的隔离命名空间重新进行了 [T1/T3 真实模型调查](evidence/windows-bridge-current-reproduction.json)：两次都使用交付的等待钩子和 Windows 桥接，三个只读工具成功执行，模型指出相应原因，释放后业务恢复。该记录包含源码哈希和清理范围；它仍是每种故障各一次的合成场景，不代表生产准确率。Linux 原生调查钩子没有在这次运行中验收。

之后在 VMware 的 Linux 节点上，使用 HolmesGPT 0.43.0 和本地模拟模型服务[验证了调查入口的工具白名单](evidence/linux-holmes-guard-fake-model.json)：允许的工具被正确传递，多余工具和修改后的配置会在模型请求前被拒绝。该测试只运行从钩子提取的 Python 部分，未执行完整 Shell 钩子、访问 Kubernetes 或调用真实模型；本机 Magpie 仅监听 Windows 回环地址，Linux 原生真实调查仍未验收。

随后在 GitHub 托管的隔离 kind 集群，以真实本地 Qwen2.5-7B 模型首次运行了 [Linux 原生调查钩子](evidence/hosted-linux-real-model-initial.json)。T1/T3 均调用了三个只读工具，但工具结果返回后的请求超过模型服务的 8192 Token 上下文，两次调查均失败，没有得到可核实的最终根因。两次故障均恢复、隔离集群已删除；这是合成实验，不代表生产准确率。

精简路由证据并增加上下文后[重新运行](evidence/hosted-linux-real-model-compact.json)：路由输出由约 27 KB 降到约 1.2–1.5 KB，本次未出现上下文超限。T1 在 300 秒时限内未完成；T3 返回了回答，但没有对照日志端口 81 和 Service 端口 80，未达到根因验收。两次故障均恢复，隔离集群已删除；这仍不是独立人工 UAT。

将隔离实验的钩子时限调整为 600 秒后，第三轮 [Linux 原生调查](evidence/hosted-linux-real-model-longer.json)的两个钩子均完成，但模型仍未正确定位：T1 错称 selector 与 Pod 标签匹配；T3 未对照 81 与 80，并建议调整超时。两次故障均已恢复，该建议没有执行。工作流成功只表示实验和恢复步骤完成，**不表示模型根因判断通过**。

补充 Service ClusterIP 和确定性标签匹配证据后的[第四轮调查](evidence/hosted-linux-real-model-grounded.json)中，T1 模型指出错误 selector 未匹配 Pod，并将配置或标签错误列为可能原因；T3 仍未识别 81 与 80 的端口差异。两次均恢复；每个场景只有一次此次运行，尚需独立人工复核，不能推断准确率。

托管实验产物只适用于合成集群：日志中的地址替换是尽力处理，无法保证覆盖跨多行地址，也可能误替换相邻数字；不要把这套日志导出流程直接用于真实集群或敏感数据。

复测中，模型虽被要求不提出修改，仍在文字中给出了修复建议；这些建议没有被执行。实际权限由只读工具和 Kubernetes RBAC 约束，恢复由操作者执行。

上述 Kubernetes 实验之后，复现脚本增加了调查钩子的上下文一致性检查。原证据保留当时的源码哈希；新版本已在同一隔离命名空间复验 T1/T3 和恢复，[新证据](evidence/context-hook-smoke.json)记录了上下文拒绝与测试钩子结果。该钩子没有调用模型。

## 故障调查流程

```text
业务请求异常 → Agent 只读取证 → 对照服务路由与日志 → 定位故障 → 人工恢复 → HTTP 验证
```

AI SRE 在本项目中的实际能力是**辅助故障诊断**：自动调查、证据关联和原因判断；变更与恢复仍由工程师执行。

## 架构

```text
业务请求 → Service/t3-gateway:8080 → Nginx Gateway
                                   → Service/t1-web:80 → Web Pod

HolmesGPT → namespace 限定的只读取证工具 → Kubernetes API / 网关日志
```

实验环境：Kubernetes v1.36.4、HolmesGPT v0.43.0、Nginx 1.25，命名空间为 `incident-lab`。Agent 仅调查，故障注入和恢复由人工执行。

## 真实故障案例

| 案例 | 故障现象 | Agent 调查结果 |
|---|---|---|
| [T1：Service Selector 不匹配](docs/t1-investigation.md) | Pod Ready，但 Service 没有可用后端 | 根据 Pod 标签、Service Selector、EndpointSlice 定位匹配错误 |
| [T3：Gateway 上游端口错误](docs/t3-investigation.md) | `/healthz` 为 200，`/api/` 为 502 | 原有工具仅定位故障范围；增加日志工具后识别端口 81 与 Service 端口 80 不匹配 |

T3 的两轮调查使用相同故障描述，Agent 自主调用工具。**增强前只判断问题范围，增强后通过日志与 Service 端口对照识别具体错误。** 人工恢复配置后，业务返回 HTTP 200。

[查看技术复盘与对照结果](docs/technical-report.md)

## 仓库内容

- [toolsets/incident-lab-readonly.yaml](toolsets/incident-lab-readonly.yaml)：三个固定范围的只读调查工具，分别读取 Pod、服务路由和 Gateway 日志。
- [k8s/incident-lab.yaml](k8s/incident-lab.yaml)：Namespace、两个 Deployment、两个 Service 与 Gateway ConfigMap。
- [docs/architecture.md](docs/architecture.md)：业务调用链、取证过程与边界。
- [docs/technical-report.md](docs/technical-report.md)：T1/T3 关键证据、实验结果及工程取舍。

服务路由工具保留 Service 的 ClusterIP、端口、selector 和 Pod 标签，并计算标签匹配结果，方便对照日志中的上游地址。标签匹配不等于端点就绪；输入缺失或不完整时不会把未知状态写成确定结论。模型仍需要正确解释这些证据。

网关日志工具还会将日志中明确的上游 IP 和端口与当前 Service 的 ClusterIP、TCP 端口对照。日志反映过去、Service 信息是之后读取的快照；端口差异只能作为调查线索，不能单独证明根因。

## 复现与验证

在专用测试集群或新建的 kind 集群运行。需要 Bash、Python 3 和 `kubectl`。脚本会创建 `incident-lab`，随后依次注入并恢复 T1/T3，不能用于有现成同名命名空间的集群。

```bash
kind create cluster --name incident-workbench
bash scripts/smoke.sh --context kind-incident-workbench
```

这一步不调用 LLM。它用短期 ServiceAccount 凭据执行取证并验证禁止权限，再检查 T1 的 EndpointSlice 无可用后端、T3 的 Nginx 日志出现错误端口，以及两次恢复后 `/api/` 均返回 200。[CI 配置](.github/workflows/incident-lab.yml)在独立 kind 集群执行同一脚本；相同版本和镜像的[本地 kind 验证](evidence/kind-workflow-local.json)已通过。首次 [GitHub Actions 运行](https://github.com/wanghaibing07/aisre-incident-workbench/actions/runs/43909333677)在 T3 未观察到预期状态而失败；增加 504 兼容和同一请求的日志证据检查后，[后续运行](https://github.com/wanghaibing07/aisre-incident-workbench/actions/runs/43911969837)通过，实际观察到 504。首次失败的具体状态未记录，不能据此断言当时也是 504。复用已部署的实验环境时也可以单独运行 `bash scripts/verify-rbac.sh --context kind-incident-workbench`。

错误上游连接被立即拒绝时可能返回 502，超时时可能返回 504；复现脚本接受两者，同时必须在本次请求对应的网关日志中观察到错误的 81 端口。此前实验记录的是实际出现的 502。

已有实验集群中如果 `incident-lab` 已存在，可以生成另一个命名空间的副本。下面示例仅把副本中的两个演示 Pod 调度到所选节点，并为它们添加控制节点容忍；不会改变节点污点或原有 `incident-lab`。先确认节点及集群是本次实验允许使用的环境：

```bash
python3 scripts/prepare-isolated.py --namespace incident-eval-demo123 --node k8s-master --output /tmp/incident-eval-demo123
bash /tmp/incident-eval-demo123/scripts/smoke.sh --context YOUR_LAB_CONTEXT
```

生成器只创建本地副本；实际创建资源由 `smoke.sh` 完成。在 Linux 上生成时，调查和等待钩子会自动取得当前用户的执行权限；如果副本从 Windows 复制过去，请在 Linux 上执行 `chmod u+x /tmp/incident-eval-demo123/scripts/{investigate,wait-investigation}.sh`。若仓库镜像代理缺少 `nginx:1.25`，应先检查镜像源或由管理员提供同版本镜像，不要把镜像拉取失败当成故障案例结果。

### Windows 桥接复现

Windows 上需要安装 HolmesGPT 0.43.0 的 Python、Git Bash 和 OpenSSH，并将本机回环地址的模型服务及密钥分别安全地设置为进程环境变量 `OPENAI_API_BASE` 和 `OPENAI_API_KEY`。远端 Linux 主机需要能用所选上下文管理实验命名空间；其 SSH 主机密钥应预先放在指定的 `known_hosts` 中。模型凭据不要写进仓库或命令参数。

先在远端 Linux 的独立终端中，使用已创建的隔离副本及全新的私有信号目录保持故障。以下以 T1 为例，目录和上下文应替换成自己的实验环境：

```bash
LAB_DIR=/tmp/incident-eval-demo123
export BRIDGE_SIGNAL_DIR=$(mktemp -d /tmp/incident-bridge-signals.XXXXXXXX)
bash "$LAB_DIR/scripts/reproduce.sh" --context YOUR_LAB_CONTEXT --case t1 --on-fault "$LAB_DIR/scripts/wait-investigation.sh"
```

看到 `READY_FOR_BRIDGE` 后，在 Windows 的项目目录运行以下命令，将地址、用户名、密钥路径、上下文和模型替换为实际值；`--remote-fixture` 必须指向远端与命名空间同名的隔离副本：

```powershell
python scripts/windows-holmes-bridge.py --case t1 --namespace incident-eval-demo123 `
  --host LAB_HOST --user LAB_USER --identity 'C:/path/to/id_key' `
  --known-hosts 'C:/path/to/known_hosts' --ssh 'C:/Windows/System32/OpenSSH/ssh.exe' `
  --bash 'C:/Program Files/Git/bin/bash.exe' --remote-fixture /tmp/incident-eval-demo123 `
  --context YOUR_LAB_CONTEXT --model 'openai/YOUR_MODEL'
```

调查结束后，在另一个远端 Linux 终端执行等待钩子打印的 `touch .../t1.done` 命令，原终端会恢复故障并验证 HTTP 状态。等待最多 240 秒，超时也会触发恢复。T3 使用 `--case t3`，并使用新的信号目录；不要复用已有信号文件。桥接只暴露三个固定只读工具，故障注入和恢复仍由远端操作者执行。

若要复跑真实调查，先在干净的 Linux 实验环境配置 HolmesGPT 0.43.0，仅注入本次所需的模型凭据，不要将凭据写进仓库。钩子使用安装 HolmesGPT 的 Python（默认 `python3`，可用 `HOLMES_PYTHON` 指定解释器），在故障证据确认后启动调查，并使用独立的临时只读 kubeconfig 和临时 Holmes 配置目录；随后由操作者脚本恢复配置。钩子禁用所有内置工具，并在模型请求前检查实际启用的工具：必须恰好为这三个固定只读工具，否则停止。此检查依赖 HolmesGPT 0.43.0 的内部接口，其他版本会被拒绝。避免在带有其他外部工具集成或凭据的进程环境运行。

以下命令适用于上面新建的 kind 集群和仓库原始的 `incident-lab`：

```bash
export LAB_CONTEXT=kind-incident-workbench
export HOLMES_MODEL='your-configured-model'
chmod +x scripts/investigate.sh
bash scripts/reproduce.sh --context "$LAB_CONTEXT" --case t1 --on-fault ./scripts/investigate.sh
bash scripts/reproduce.sh --context "$LAB_CONTEXT" --case t3 --on-fault ./scripts/investigate.sh
```

若使用上面创建的隔离副本，必须改用副本里的两个脚本，不要运行仓库中的原始脚本：

```bash
export LAB_CONTEXT=YOUR_LAB_CONTEXT
export HOLMES_MODEL='your-configured-model'
LAB_DIR=/tmp/incident-eval-demo123
bash "$LAB_DIR/scripts/reproduce.sh" --context "$LAB_CONTEXT" --case t1 --on-fault "$LAB_DIR/scripts/investigate.sh"
bash "$LAB_DIR/scripts/reproduce.sh" --context "$LAB_CONTEXT" --case t3 --on-fault "$LAB_DIR/scripts/investigate.sh"
```

用工具调用和返回的证据验收模型结果：T1 需指出 Service Selector 和 Pod 标签不匹配；T3 需比较日志的 81 与 Service 的 80。模型调用成功退出并不代表根因判断通过。历史结果见上方案例，和新脚本的复跑记录分别统计。

调查钩子默认限时 300 秒；在隔离实验中使用较慢的本地模型时，可随 `--on-fault` 显式传入 `--hook-timeout 600`，允许范围为 1–900 秒。超时后脚本仍会终止钩子并尝试恢复故障；时限越长，故障保持时间也越长。

在安装 HolmesGPT 0.43.0 的 Python 环境中运行 `python tests/verify_holmes_guard.py`，可用本机模拟模型服务检查工具白名单和拒绝行为，不需要 Kubernetes 集群或模型密钥；这不能替代真实调查验收。

## 设计重点

复用 HolmesGPT 进行调查；围绕 T3 故障补充 Nginx 日志取证工具，使 Agent 从定位异常链路进一步识别错误端口。调查权限限定为只读，恢复操作由工程师控制。
