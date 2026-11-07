{{- define "ewallet-edge.labels" -}}
app.kubernetes.io/part-of: ewallet-edge
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "ewallet-edge.hosts" -}}
{{- $hosts := list }}
{{- range $name, $route := .Values.routes }}
{{- if $route.enabled }}{{ $hosts = append $hosts (printf "%s.%s" $route.hostPrefix $.Values.domain) }}{{ end }}
{{- end }}
{{- toJson $hosts }}
{{- end }}
