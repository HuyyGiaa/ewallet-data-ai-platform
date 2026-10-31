{{- define "llm-agent-platform.labels" -}}
app.kubernetes.io/part-of: ewallet-llm-agent
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* Enforced flows, plus the llm-d flows when inference routing is enabled. */}}
{{- define "llm-agent-platform.enforcedFlows" -}}
{{- $flows := .Values.networkFlows.enforced -}}
{{- if .Values.llmd.enabled -}}
{{- $flows = concat $flows .Values.llmd.networkFlows -}}
{{- end -}}
{{- toYaml $flows -}}
{{- end -}}
