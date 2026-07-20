#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_dir}/../../.." && pwd)"
manifest="${script_dir}/arxiv-pdf-ids.txt"
destination="${1:-${repository_root}/arxiv-pdfs}"
base_url="https://export.arxiv.org/pdf"
user_agent="vectorless-rag/0.1 (+https://github.com/gcharang/rag-agent-vectorless)"
current_temporary_file=""

cleanup() {
	if [[ -n "${current_temporary_file}" && -e "${current_temporary_file}" ]]; then
		rm -f -- "${current_temporary_file}"
	fi
}

fail() {
	printf 'error: %s\n' "$1" >&2
	exit 2
}

has_pdf_signature() {
	[[ -f "$1" ]] && [[ "$(LC_ALL=C head -c 5 -- "$1" 2>/dev/null)" == "%PDF-" ]]
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$#" -le 1 ]] || fail "usage: ${0##*/} [destination]"
command -v curl >/dev/null 2>&1 || fail "curl is required"
[[ -r "${manifest}" ]] || fail "manifest is not readable: ${manifest}"

identifiers=()
while IFS= read -r identifier || [[ -n "${identifier}" ]]; do
	[[ "${identifier}" =~ ^[0-9]{4}\.[0-9]{4,5}$ ]] ||
		fail "invalid arXiv ID in manifest: ${identifier}"
	identifiers+=("${identifier}")
done <"${manifest}"

[[ "${#identifiers[@]}" -gt 0 ]] || fail "manifest is empty: ${manifest}"
duplicates="$(printf '%s\n' "${identifiers[@]}" | sort | uniq -d)"
[[ -z "${duplicates}" ]] || fail "duplicate arXiv ID in manifest: ${duplicates%%$'\n'*}"

if ! mkdir -p -- "${destination}"; then
	fail "could not create destination: ${destination}"
fi
[[ -d "${destination}" ]] || fail "destination is not a directory: ${destination}"
[[ -w "${destination}" ]] || fail "destination is not writable: ${destination}"

downloaded=0
skipped=0
failed=0
requests=0

for identifier in "${identifiers[@]}"; do
	target="${destination}/${identifier}.pdf"

	if [[ -e "${target}" || -L "${target}" ]]; then
		if has_pdf_signature "${target}"; then
			skipped=$((skipped + 1))
		else
			printf 'error: existing path is not a valid PDF: %s\n' "${target}" >&2
			failed=$((failed + 1))
		fi
		continue
	fi

	if ! current_temporary_file="$(mktemp "${destination}/.${identifier}.XXXXXX.part")"; then
		printf 'error: could not create a temporary file for %s\n' "${identifier}" >&2
		failed=$((failed + 1))
		continue
	fi

	printf 'download %s\n' "${identifier}"
	requests=$((requests + 1))
	if ! curl \
		--fail \
		--location \
		--retry 3 \
		--retry-all-errors \
		--show-error \
		--silent \
		--user-agent "${user_agent}" \
		--output "${current_temporary_file}" \
		"${base_url}/${identifier}"; then
		printf 'error: download failed for %s\n' "${identifier}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	elif ! has_pdf_signature "${current_temporary_file}"; then
		printf 'error: response for %s is not a PDF\n' "${identifier}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	elif ! chmod 0644 "${current_temporary_file}"; then
		printf 'error: could not install %s\n' "${target}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	elif [[ -e "${target}" || -L "${target}" ]]; then
		printf 'error: destination appeared during download: %s\n' "${target}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	elif ! mv -n -- "${current_temporary_file}" "${target}"; then
		printf 'error: could not install %s\n' "${target}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	elif [[ -e "${current_temporary_file}" ]]; then
		printf 'error: destination appeared during download: %s\n' "${target}" >&2
		rm -f -- "${current_temporary_file}"
		current_temporary_file=""
		failed=$((failed + 1))
	else
		current_temporary_file=""
		downloaded=$((downloaded + 1))
	fi

	if ((requests % 4 == 0)); then
		sleep 1
	fi
done

printf 'complete: downloaded=%d skipped=%d failed=%d destination=%s\n' \
	"${downloaded}" "${skipped}" "${failed}" "${destination}"

((failed == 0))
