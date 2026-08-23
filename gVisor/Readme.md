# Get gVisor env @windows

## 0. create WSL
```
# install
$ wsl --install -d Ubuntu-24.04

# set default 
$ wsl --set-default Ubuntu-24.04

# execute default wsl
$ wsl
To run a command as administrator (user "root"), use "sudo <command>".
See "man sudo_root" for details.
ghun@DESKTOP-GHUN-SYS:/mnt/d/project/withClaude/llmops/gVisor$

# test runsc
$ mkdir -p ~/bin && cd ~/bin && ARCH=$(uname -m) && \
    wget -q https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc && \
    chmod +x runsc && ./runsc --version
runsc version release-20260817.0
spec: 1.2.1
$ rm ./runsc

```

## 1. Install containerd(ctr) + runc
```
# install containerd, runc
$ sudo apt-get update && sudo apt-get install -y containerd runc

$ sudo systemctl enable --now containerd && systemctl is-active containerd
tainerd
active

```

## 2. Install runsc + shim
```
# install runsc, shim with the latest version
$ cd /tmp && ARCH=$(uname -m) && \
    URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH} && \
    wget -q ${URL}/runsc ${URL}/runsc.sha512 ${URL}/containerd-shim-runsc-v1 ${URL}/containerd-shim-runsc-v1.sha512 && \
    sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512

# add role & move /usr/local/bin
$ chmod a+rx /tmp/runsc /tmp/containerd-shim-runsc-v1 && \
    sudo mv /tmp/runsc /tmp/containerd-shim-runsc-v1 /usr/local/bin/

```

## 3. Edit & apply config.toml to containerd(ctr, crictl)
```
# set default config
$ sudo mkdir -p /etc/containerd && \
    containerd config default | sudo tee /etc/containerd/config.toml > /dev/null

# 1. update as systemCgroup = true
$ sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml

# 2. add runsc as plugin (containerd 2.2.1)
$ sudo tee -a /etc/containerd/config.toml > /dev/null <<'EOF'

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = 'io.containerd.runsc.v1'
EOF


# restart containerd
$ sudo systemctl restart containerd

$ sudo ctr version
Client:
  Version:  2.2.1
  Revision: 
  Go version: go1.24.4

Server:
  Version:  2.2.1
  Revision: 
  UUID: 29a0d2fe-7586-4d87-a591-2d554a1bca77

```

## 4. containerd(ctr)
- ctr: act with containerd core with config.toml
- crictl: act with cri plugin, config.toml (like k8s)

```
# 1. Check ctr
$ sudo ctr images pull docker.io/library/alpine:3

# check process info. (gvisor)
$ sudo ctr run --rm -t --runtime io.containerd.runsc.v1 docker.io/library/alpine:3 gv-test sh
/ # cat /proc/version
Linux version 4.19.0-gvisor #1 SMP Sun Jan 10 15:06:54 PST 2016
/ # exit

$ sudo ctr containers ls
$ sudo ctr container delete gv-test

```

## 5. crictl (like k8s)
1. make pod with crictl 
```
# 1.install crictl 
$ VER=v1.32.0 && \
    cd /tmp && wget -q https://github.com/kubernetes-sigs/cri-tools/releases/download/${VER}/crictl-${VER}-linux-amd64.tar.gz && \
    sudo tar -C /usr/local/bin -xzf crictl-${VER}-linux-amd64.tar.gz

# 2. add crictl.yaml
$ sudo tee /etc/crictl.yaml > /dev/null <<'EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
EOF

# 3. make pod
# set network = 2 as NODE without installing CNI
$ cat > /tmp/pod.json <<'EOF'
{
  "metadata": { "name": "gv-pod", "namespace": "default", "uid": "gv-pod-uid" },
  "log_directory": "/tmp",
  "linux": {
    "security_context": {
      "namespace_options": { "network": 2 }
    }
  }
}
EOF

# 4. run pod
$ sudo crictl runp --runtime runsc /tmp/pod.json
aeb80438efca0975e5fb4c28a2784015dd8927695f89f723d18a87a8b5e36105

# 5. check pod info.
$ sudo containerd config dump | grep -B2 -A6 "runtimes.runsc"
$ sudo crictl info | grep -A5 runsc
$ sudo crictl pods
POD ID              CREATED             STATE               NAME                NAMESPACE           ATTEMPT             RUNTIME
aeb80438efca0       23 minutes ago      Ready               gv-pod              default             0                   runsc

$ sudo crictl stopp aeb80438efca0 && sudo crictl rmp aeb80438efca0

```
2. make container with crictl 
```
# create pod
$ sudo crictl runp --runtime runsc /tmp/pod.json

# get pods id
$ sudo crictl pods

# make container json
$ cat > /tmp/container.json <<'EOF'
{
  "metadata": { "name": "gv-shell" },
  "image": { "image": "docker.io/library/alpine:3" },
  "command": ["sleep", "3600"],
  "log_path": "gv-shell.log",
  "linux": {}
}
EOF
```

3. CASE1: STEP BY STEP
```
# get image @crictl
$ sudo crictl pull docker.io/library/alpine:3

# create container on the pod
$ sudo crictl create e2219e24eff95 /tmp/container.json /tmp/pod.json
189431cf68d4b60fdfbd8e5e48484fd30676b666bacafb10bf878909c6ba57d8

# start container
$ sudo crictl start 189431cf68d4b60fdfbd8e5e48484fd30676b666bacafb10bf878909c6ba57d8

$ sudo crictl ps
CONTAINER           IMAGE                        CREATED             STATE               NAME                ATTEMPT             POD ID              POD                 NAMESPACE
189431cf68d4b       docker.io/library/alpine:3   2 minutes ago       Running             gv-shell            0                   e2219e24eff95       unknown             unknown

# exec in container
$ sudo crictl exec -it 189431cf68d4b sh
```

4. CASE2: ALL IN ONE
```
# run as all in one
$ sudo crictl run --runtime runsc /tmp/container.json /tmp/pod.json

```

5. finish
```
# delete all resources
$ sudo crictl rmp -fa
Stopped sandbox e2219e24eff955518b6a8012cce54db536a6b03bbf9e51d6a1635f210df86d9e
Removed sandbox e2219e24eff955518b6a8012cce54db536a6b03bbf9e51d6a1635f210df86d9e

```

# 6. run crictl as gVisor
run python gVisor in WSL, NOT Pod

```
# check pysandbox
$ python demo.py

# run python code in sandbox
$ python sandbox.py -c "import platform; print(platform.release())"


```


# 7. run pod, container in k8s
1. architecture
```
              ┌─ 디스패처 ─┐
요청 ────────>│ 유휴 파드   │───> 코드 전송 ──> 결과
              │ 하나 점유   │
              └─────┬──────┘
                    │ 실행 후
                    ▼
              파드 삭제 → ReplicaSet이 새로 채움
```
ex) LangGraph ──(HTTP)──> 샌드박스 서비스(디스패처) ──(k8s API)──> 파드


2. runtime class for gVisor
```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
scheduling:
  nodeSelector:
    sandbox: gvisor
  tolerations:
    - key: sandbox
      operator: Equal
      value: gvisor
      effect: NoSchedule
overhead:
  podFixed:
    cpu: "50m"
    memory: "128Mi"
```

3. namespace
restricted 프로필이 hostPath, hostNetwork, privileged를 네임스페이스 차원에서 봉쇄합니다. 파드 스펙에서 실수로 열어도 거부됩니다.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: sandbox
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest

```
4. service account
권한 없는 전용
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: sandbox-runner
  namespace: sandbox
automountServiceAccountToken: false
```

5. deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sandbox-pool
  namespace: sandbox
spec:
  replicas: 10
  # 풀 크기를 유지하며 교체. 롤아웃 중에도 가용 파드가 줄지 않습니다.
  strategy:
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 2
  selector:
    matchLabels:
      app: sandbox
  template:
    metadata:
      labels:
        app: sandbox        # 셀렉터가 보는 라벨
        status: idle        # 점유 표시용. 셀렉터에 넣지 않는 것이 중요 (아래 설명)
    spec:
      runtimeClassName: gvisor
      serviceAccountName: sandbox-runner
      automountServiceAccountToken: false
      # 클러스터의 모든 Service를 환경변수로 주입하는 기본 동작을 끕니다.
      # 신뢰할 수 없는 코드에 클러스터 지형을 알려줄 이유가 없습니다.
      enableServiceLinks: false
      terminationGracePeriodSeconds: 5
      # 파드의 최대 수명. 유휴 상태에서도 카운트되므로 짧게 잡으면 안 됩니다.
      activeDeadlineSeconds: 3600
      # 풀이 한 노드에 몰리면 노드 하나가 빠질 때 풀 전체가 흔들립니다.
      topologySpreadConstraints:
        - maxSkew: 2
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: sandbox
      containers:
        - name: agent
          image: sandbox-agent:1.0
          # 노드에 이미 있으면 받지 않습니다. 콜드스타트의 대부분이 이미지 풀입니다.
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          # 디스패처가 Ready인 파드만 점유하도록. 풀에서는 필수입니다.
          readinessProbe:
            httpGet: { path: /healthz, port: http }
            periodSeconds: 2
            failureThreshold: 2
          resources:
            requests: { cpu: "500m", memory: 512Mi }
            limits:   { cpu: "500m", memory: 512Mi }   # 동일 → Guaranteed QoS
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000          # 이미지에 USER가 없으면 runAsNonRoot만으로는 기동 실패
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
            seccompProfile:
              type: RuntimeDefault   # PodSecurity restricted의 요구사항
          volumeMounts:
            - { name: work, mountPath: /work }
            - { name: tmp,  mountPath: /tmp }
      volumes:
        - name: work
          emptyDir: { medium: Memory, sizeLimit: 256Mi }
        - name: tmp
          emptyDir: { medium: Memory, sizeLimit: 64Mi }
```

6. restricted network policy
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sandbox-default-deny
  namespace: sandbox
spec:
  podSelector: { matchLabels: { app: sandbox } }
  policyTypes: [Ingress, Egress]
  ingress:
    # 디스패처만 파드에 접속할 수 있습니다.
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: dispatcher } }
          podSelector: { matchLabels: { app: dispatcher } }
      ports:
        - { protocol: TCP, port: 8080 }
  egress: []      # 나가는 트래픽 전면 차단
```


