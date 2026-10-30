{{- define "llm-agent-platform.labels" -}}
app.kubernetes.io/part-of: ewallet-llm-agent
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
