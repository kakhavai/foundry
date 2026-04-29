{{- define "generic-service.fullname" -}}
{{- .Release.Name }}
{{- end }}

{{- define "generic-service.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- include "generic-service.selectorLabels" . | nindent 0 }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "generic-service.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.service.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
