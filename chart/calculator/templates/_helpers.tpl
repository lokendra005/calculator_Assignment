{{- define "calculator.labels" -}}
app.kubernetes.io/name: calculator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
