{{/* Base name, overridable. */}}
{{- define "momentum.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "momentum.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Common labels. */}}
{{- define "momentum.labels" -}}
app.kubernetes.io/name: {{ include "momentum.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/* Component-scoped selector labels. Call with (dict "ctx" . "component" "app"). */}}
{{- define "momentum.selectorLabels" -}}
app.kubernetes.io/name: {{ include "momentum.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Names of shared resources. */}}
{{- define "momentum.postgresName" -}}{{ printf "%s-postgres" (include "momentum.fullname" .) }}{{- end -}}
{{- define "momentum.appName" -}}{{ printf "%s-app" (include "momentum.fullname" .) }}{{- end -}}
{{- define "momentum.grafanaName" -}}{{ printf "%s-grafana" (include "momentum.fullname" .) }}{{- end -}}
{{- define "momentum.configMapName" -}}{{ printf "%s-config" (include "momentum.fullname" .) }}{{- end -}}
{{- define "momentum.appDataPvcName" -}}{{ printf "%s-app-data" (include "momentum.fullname" .) }}{{- end -}}

{{/* Name of the Secret holding DB URL + broker keys (existing or chart-managed). */}}
{{- define "momentum.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "momentum.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the Postgres password once: explicit value, else reuse the one already
stored in the chart-managed Secret (so upgrades don't rotate it), else generate.
*/}}
{{- define "momentum.postgresPassword" -}}
{{- if .Values.postgres.password -}}
{{- .Values.postgres.password -}}
{{- else -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (printf "%s-secrets" (include "momentum.fullname" .)) -}}
{{- if and $existing $existing.data (index $existing.data "POSTGRES_PASSWORD") -}}
{{- index $existing.data "POSTGRES_PASSWORD" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}
