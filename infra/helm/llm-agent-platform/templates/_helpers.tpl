{{- define "llm-agent-platform.labels" -}}
app.kubernetes.io/part-of: ewallet-llm-agent
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* Enforced flows, plus the llm-d, MCP Gateway and collector flows when those are enabled. */}}
{{- define "llm-agent-platform.enforcedFlows" -}}
{{- $flows := .Values.networkFlows.enforced -}}
{{- if .Values.llmd.enabled -}}
{{- $flows = concat $flows .Values.llmd.networkFlows -}}
{{- end -}}
{{- if .Values.mcpGateway.enabled -}}
{{- $flows = concat $flows .Values.mcpGateway.networkFlows -}}
{{- end -}}
{{- if .Values.otelCollector.enabled -}}
{{- $flows = concat $flows .Values.otelCollector.networkFlows -}}
{{- end -}}
{{- toYaml $flows -}}
{{- end -}}

{{/* Deployment, Service and PDB of a standalone agentgateway policy gateway. */}}
{{- define "llm-agent-platform.policyGateway" -}}
{{- $root := .root -}}
{{- $g := .gateway -}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .name }}
  namespace: {{ $root.Values.namespace.name }}
  labels:
    {{- include "llm-agent-platform.labels" $root | nindent 4 }}
    app.kubernetes.io/name: {{ .name }}
spec:
  replicas: {{ $g.replicas }}
  strategy:
    type: RollingUpdate
    rollingUpdate: {maxUnavailable: 0, maxSurge: 1}
  selector:
    matchLabels: {app.kubernetes.io/name: {{ .name }}}
  template:
    metadata:
      labels: {app.kubernetes.io/name: {{ .name }}}
      annotations:
        checksum/config: {{ .checksum }}
    spec:
      serviceAccountName: {{ .name }}
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: agentgateway
          image: {{ $root.Values.agentgateway.image }}
          imagePullPolicy: IfNotPresent
          args: ["-f", "/config/config.yaml"]
          ports:
            - {name: http, containerPort: {{ $g.port }}}
          readinessProbe:
            tcpSocket: {port: http}
            periodSeconds: 5
          livenessProbe:
            tcpSocket: {port: http}
            periodSeconds: 15
            failureThreshold: 3
          resources:
            {{- toYaml $g.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            capabilities: {drop: [ALL]}
            seccompProfile: {type: RuntimeDefault}
          volumeMounts:
            - {name: config, mountPath: /config, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: config
          configMap: {name: {{ .name }}-config}
        - name: tmp
          emptyDir: {sizeLimit: 64Mi}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ .name }}
  namespace: {{ $root.Values.namespace.name }}
  labels:
    {{- include "llm-agent-platform.labels" $root | nindent 4 }}
spec:
  type: ClusterIP
  selector: {app.kubernetes.io/name: {{ .name }}}
  ports:
    - {name: http, port: {{ $g.port }}, targetPort: http}
{{- if gt (int $g.replicas) 1 }}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .name }}
  namespace: {{ $root.Values.namespace.name }}
  labels:
    {{- include "llm-agent-platform.labels" $root | nindent 4 }}
spec:
  minAvailable: 1
  selector:
    matchLabels: {app.kubernetes.io/name: {{ .name }}}
{{- end }}
{{- end -}}

{{/* Duration ("250ms", "30s", "2m") to integer milliseconds. */}}
{{- define "llm-agent-platform.durationMs" -}}
{{- $v := toString . -}}
{{- if hasSuffix "ms" $v -}}{{ trimSuffix "ms" $v | int }}
{{- else if hasSuffix "s" $v -}}{{ mul (trimSuffix "s" $v | int) 1000 }}
{{- else if hasSuffix "m" $v -}}{{ mul (trimSuffix "m" $v | int) 60000 }}
{{- else -}}{{ fail (printf "unsupported duration %q" $v) }}
{{- end -}}
{{- end -}}
