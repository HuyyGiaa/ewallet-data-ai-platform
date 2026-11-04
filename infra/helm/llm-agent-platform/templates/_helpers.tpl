{{- define "llm-agent-platform.labels" -}}
app.kubernetes.io/part-of: ewallet-llm-agent
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* Enforced flows, plus the llm-d, policy-gateway and collector flows when enabled. */}}
{{- define "llm-agent-platform.enforcedFlows" -}}
{{- $flows := .Values.networkFlows.enforced -}}
{{- if .Values.llmd.enabled -}}
{{- if eq .Values.llmd.mode "standalone" -}}
{{- $flows = concat $flows .Values.llmd.standalone.networkFlows -}}
{{- else -}}
{{- $flows = concat $flows .Values.llmd.networkFlows -}}
{{- end -}}
{{- end -}}
{{- $mcpBackendNamespace := default .Values.namespace.name .Values.mcpGateway.backendNamespace -}}
{{- if and .Values.mcpGateway.enabled (eq $mcpBackendNamespace .Values.namespace.name) -}}
{{- $flows = concat $flows .Values.mcpGateway.networkFlows -}}
{{- end -}}
{{- if .Values.a2aGateway.enabled -}}
{{- $flows = concat $flows .Values.a2aGateway.networkFlows -}}
{{- end -}}
{{- if .Values.otelCollector.enabled -}}
{{- $flows = concat $flows .Values.otelCollector.networkFlows -}}
{{- end -}}
{{- if .Values.observabilityStack.enabled -}}
{{- $flows = concat $flows .Values.observabilityStack.networkFlows -}}
{{- if and .Values.llmd.enabled (eq .Values.llmd.mode "standalone") -}}
{{- $flows = concat $flows .Values.observabilityStack.llmdStandaloneNetworkFlows -}}
{{- end -}}
{{- end -}}
{{- if .Values.idempotencyRedis.enabled -}}
{{- $flows = concat $flows .Values.idempotencyRedis.networkFlows -}}
{{- end -}}
{{- if (dig "enabled" false (.Values.podLogs | default dict)) -}}
{{- $flows = append $flows (dict "name" "observability-alloy-to-loki" "from" "observability-alloy" "to" "observability-loki" "port" 3100) -}}
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
      {{- with $root.Values.llmPlacement.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
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

{{/* N11: agentgateway v1.7.0 `config.tracing` (validated with --validate-only).
Spans go to the shared collector over the existing gateway -> otel-collector:4317
flows; incoming W3C traceparent is honoured. Rendered only when opted in. */}}
{{- define "llm-agent-platform.gatewayTracing" -}}
{{- if and .Values.otelCollector.enabled (dig "enabled" false (.Values.gatewayTracing | default dict)) }}
  tracing:
    otlpEndpoint: http://otel-collector.{{ .Values.namespace.name }}.svc.cluster.local:4317
    otlpProtocol: grpc
    randomSampling: "true"
{{- end }}
{{- end }}

{{/* N12: Alloy pipeline for templates/pod-logs.yaml (Kubernetes API -> redact -> Loki). */}}
{{- define "llm-agent-platform.alloyConfig" -}}
{{- $root := .root -}}
{{- $keep := list -}}
{{- range .sources -}}
{{- $keep = append $keep (printf "%s;%s;(%s)" .namespace .label (join "|" .values)) -}}
{{- end -}}
discovery.kubernetes "pods" {
  role = "pod"
  namespaces {
    names = [{{ range $i, $n := .namespaces }}{{ if $i }}, {{ end }}{{ $n | quote }}{{ end }}]
  }
}

discovery.relabel "selected" {
  targets = discovery.kubernetes.pods.targets
  {{- range .sources }}
  {{- $label := regexReplaceAll "[^A-Za-z0-9]" .label "_" }}
  rule {
    source_labels = ["__meta_kubernetes_namespace", "__meta_kubernetes_pod_label_{{ $label }}"]
    separator     = ";"
    regex         = {{ printf "%s;(%s)" .namespace (join "|" .values) | quote }}
    target_label  = "service_name"
    replacement   = "$1"
  }
  {{- end }}
  // Only pods matched by a source rule above carry service_name.
  rule {
    source_labels = ["service_name"]
    regex         = ".+"
    action        = "keep"
  }
  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    target_label  = "k8s_namespace_name"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_name"]
    target_label  = "k8s_pod_name"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_container_name"]
    target_label  = "k8s_container_name"
  }
  rule {
    target_label = "log_source"
    replacement  = "kubernetes-api"
  }
}

loki.source.kubernetes "pods" {
  targets    = discovery.relabel.selected.output
  forward_to = [loki.process.redact.receiver]
}

// Defence in depth: these access logs carry no credentials today, but any
// bearer token, API key, email, Vietnamese mobile number or grouped card
// number is masked before it leaves the pod.
loki.process "redact" {
  forward_to = [loki.write.loki.receiver]

  stage.replace {
    expression = "(?i)(?:bearer\\s+|api[-_]?key[\"']?\\s*[:=]\\s*[\"']?|x-ewallet-mcp-service-token[\"']?\\s*[:=]\\s*[\"']?)([A-Za-z0-9._~+/=-]{8,})"
    replace    = "<redacted>"
  }
  stage.replace {
    expression = "([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)*\\.[A-Za-z]{2,})"
    replace    = "<redacted-email>"
  }
  stage.replace {
    expression = "(?:^|[^0-9.])((?:\\+84|84|0)[35789][0-9]{8})(?:[^0-9]|$)"
    replace    = "<redacted-phone>"
  }
  stage.replace {
    expression = "(?:^|[^0-9.])([0-9]{4}[ -][0-9]{4}[ -][0-9]{4}[ -][0-9]{1,7})(?:[^0-9]|$)"
    replace    = "<redacted-card>"
  }
}

loki.write "loki" {
  endpoint {
    url = "http://loki.{{ $root.Values.namespace.name }}.svc.cluster.local:3100/loki/api/v1/push"
  }
}
{{- end -}}
