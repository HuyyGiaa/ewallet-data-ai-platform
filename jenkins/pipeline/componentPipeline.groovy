def run(Map configuration = [:]) {
  String profile = configuration.get('profile', 'platform')
  properties([
    disableConcurrentBuilds(),
    parameters([
      string(name: 'EXPECTED_GIT_SHA', defaultValue: '', description: 'Exact reviewed 40-character Git SHA'),
      string(name: 'TARGET_BRANCH', defaultValue: 'origin/main', description: 'Fetched target ref used only for merge-base change detection'),
      booleanParam(name: 'DEPLOY_APPROVED', defaultValue: false, description: 'Explicit request to enter the separately approved release gate'),
      choice(name: 'ENVIRONMENT', choices: ['dev', 'gcp', 'gke-cpu'], description: 'Validated release target'),
      string(name: 'KUBE_CONTEXT', defaultValue: '', description: 'Explicit context; the kubeconfig default is never used'),
      string(name: 'IMAGE_MANIFEST', defaultValue: '', description: 'Reviewed immutable image manifest in this checkout')
    ])
  ])

  String evidenceDir = "evidence/ci/${env.BUILD_TAG ?: 'unknown-build'}"
  timeout(time: 60, unit: 'MINUTES') {
    try {
      stage('Checkout and provenance') {
        sh """#!/usr/bin/env bash
          set -euo pipefail
          test -n "$EXPECTED_GIT_SHA"
          test "\$(git rev-parse HEAD)" = "$EXPECTED_GIT_SHA"
          mkdir -p '${evidenceDir}'
          git status --short --branch > '${evidenceDir}/git-status.txt'
          printf '%s\n' 'jenkins_runtime_verified=false' > '${evidenceDir}/jenkins-runtime-status.txt'
        """
      }

      stage('Validate catalog and detect scope') {
        withEnv(["TARGET_REF=${params.TARGET_BRANCH}"]) {
          sh """#!/usr/bin/env bash
            set -euo pipefail
            python3 -m scripts.ci.catalog validate > '${evidenceDir}/catalog-validation.json'
            python3 -m scripts.ci.change_detection \
              --root "$WORKSPACE" \
              --head "$EXPECTED_GIT_SHA" \
              --target-ref "$TARGET_REF" \
              --output '${evidenceDir}/scope.json'
          """
        }
      }

      stage('Component CI') {
        String profileArgument = profile == 'platform' ? '' : "--profile '${profile}'"
        sh """#!/usr/bin/env bash
          set -euo pipefail
          python3 -m scripts.ci.run_ci \
            --root "$WORKSPACE" \
            --scope '${evidenceDir}/scope.json' \
            --evidence-dir '${evidenceDir}' \
            ${profileArgument}
        """
      }

      if (params.DEPLOY_APPROVED) {
        stage('Controlled release gate') {
          String approvedBy = input(
            message: "Approve immutable release of ${params.EXPECTED_GIT_SHA} to ${params.ENVIRONMENT}?",
            ok: 'Approve release',
            submitterParameter: 'APPROVED_BY'
          )
          withEnv([
            "RELEASE_APPROVED_BY=${approvedBy}",
            "RELEASE_PROFILE=${profile}",
            "RELEASE_BRANCH=${env.BRANCH_NAME ?: ''}"
          ]) {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              test -n "$KUBE_CONTEXT"
              test -n "$IMAGE_MANIFEST"
              test -f "$IMAGE_MANIFEST"
              python3 - '${evidenceDir}/scope.json' '${evidenceDir}/release-request.json' <<'PY'
import json
import os
import sys

from scripts.ci.catalog import load_catalog

scope = json.load(open(sys.argv[1], encoding="utf-8"))
catalog = load_catalog("jenkins/catalog/components.json")
selected = scope["components"]
profile = os.environ["RELEASE_PROFILE"]
if profile != "platform":
    selected = [name for name in selected if name in catalog.profiles[profile]]
request = {
    "requested": True,
    "owner_approved": True,
    "owner_approval": os.environ["RELEASE_APPROVED_BY"],
    "branch": os.environ["RELEASE_BRANCH"],
    "git_sha": os.environ["EXPECTED_GIT_SHA"],
    "checkout_sha": os.environ["EXPECTED_GIT_SHA"],
    "environment": os.environ["ENVIRONMENT"],
    "kube_context": os.environ["KUBE_CONTEXT"],
    "components": selected,
    "image_manifest_path": os.environ["IMAGE_MANIFEST"],
    "evidence_dir": "${evidenceDir}/release",
}
with open(sys.argv[2], "w", encoding="utf-8") as output:
    json.dump(request, output, indent=2, sort_keys=True)
    output.write("\\n")
PY
              python3 -m scripts.ci.release_policy \
                --root "$WORKSPACE" \
                build \
                --request '${evidenceDir}/release-request.json' \
                --image-manifest "$IMAGE_MANIFEST" \
                --output '${evidenceDir}/release-plan.json'
              scripts/ci/deploy_release.sh \
                --plan '${evidenceDir}/release-plan.json' \
                --execute
            """
          }
        }
      }
    } finally {
      junit allowEmptyResults: true, testResults: "${evidenceDir}/**/junit*.xml"
      archiveArtifacts artifacts: "${evidenceDir}/**", allowEmptyArchive: false, fingerprint: true
    }
  }
}

return this
