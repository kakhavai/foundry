{{- define "github-stats.fullname" -}}
{{- .Release.Name }}-github-stats
{{- end }}

{{- define "github-stats.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{ include "github-stats.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "github-stats.selectorLabels" -}}
app.kubernetes.io/name: github-stats
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
