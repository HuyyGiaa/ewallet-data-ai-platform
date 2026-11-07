{{- define "ewallet-serving.labels" -}}
app.kubernetes.io/part-of: ewallet-mlops
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
