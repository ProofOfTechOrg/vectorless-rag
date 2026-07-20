{{- define "vectorless-rag.name" -}}vectorless-rag{{- end }}
{{- define "vectorless-rag.fullname" -}}{{ .Release.Name }}-vectorless-rag{{- end }}
{{- define "vectorless-rag.labels" -}}
app.kubernetes.io/name: {{ include "vectorless-rag.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
{{- define "vectorless-rag.postgresHost" -}}
{{- if .Values.postgresql.bundled -}}{{ include "vectorless-rag.fullname" . }}-postgres{{- else -}}{{ required "postgresql.externalHost is required" .Values.postgresql.externalHost }}{{- end -}}
{{- end }}
{{- define "vectorless-rag.clickhouseHost" -}}
{{- if .Values.clickhouse.bundled -}}{{ include "vectorless-rag.fullname" . }}-clickhouse{{- else -}}{{ required "clickhouse.externalHost is required" .Values.clickhouse.externalHost }}{{- end -}}
{{- end }}
{{- define "vectorless-rag.redisHost" -}}
{{- if .Values.redis.bundled -}}{{ include "vectorless-rag.fullname" . }}-redis{{- else -}}{{ required "redis.externalHost is required" .Values.redis.externalHost }}{{- end -}}
{{- end }}
{{- define "vectorless-rag.s3Endpoint" -}}
{{- if .Values.s3.bundled -}}http://{{ include "vectorless-rag.fullname" . }}-minio:9000{{- else -}}{{ required "s3.externalEndpoint is required" .Values.s3.externalEndpoint }}{{- end -}}
{{- end }}

