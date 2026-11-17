// Shared EDAI-2 pipeline: CI for the changed components, then (only when explicitly approved)
// a release of one release group: plan -> build or reuse images -> scan -> rehearsal, and an
// optional, separately approved production promotion. All policy lives in scripts/ci/*.py;
// this file only orders the stages and passes Jenkins parameters through.
def run(Map configuration) {
  String profile = configuration.get('profile', 'platform')
  // Job DSL owns parameter definitions. Running the properties step here replaced the
  // seeded defaults after the first build (2026-11-03 runtime finding), so a
  // Jenkinsfile only declares parameters when its job has no seed definition.
  if (configuration.get('declareParameters', false)) {
    properties([
      disableConcurrentBuilds(),
      parameters([
        string(name: 'EXPECTED_GIT_SHA', defaultValue: '', description: 'Exact reviewed 40-character Git SHA'),
        string(name: 'TARGET_BRANCH', defaultValue: 'origin/main', description: 'Fetched target ref used only for merge-base change detection'),
        string(name: 'RELEASE_BRANCH', defaultValue: '', description: 'release/<group> ref that must resolve to EXPECTED_GIT_SHA before any release'),
        booleanParam(name: 'INTENTIONAL_GATE_FAILURE', defaultValue: false, description: 'Runtime proof only: fail before every release action'),
        booleanParam(name: 'DEPLOY_APPROVED', defaultValue: false, description: 'Explicit request to enter the separately approved release gate'),
        choice(name: 'ENVIRONMENT', choices: ['gke-cpu', 'gcp', 'dev'], description: 'Validated cluster profile'),
        string(name: 'KUBE_CONTEXT', defaultValue: '', description: 'Explicit context; the kubeconfig default is never used'),
        string(name: 'IMAGE_MANIFEST', defaultValue: '', description: 'Reviewed immutable image manifest; empty builds or reuses images with Cloud Build'),
        choice(name: 'REGISTRY_LOOKUP', choices: ['required', 'off'], description: 'Artifact Registry lookup for image reuse and digest readback'),
        booleanParam(name: 'PROMOTE_TO_PRODUCTION', defaultValue: false, description: 'After a passing rehearsal, request the separately approved production promotion'),
        string(name: 'PRODUCTION_KUBE_CONTEXT', defaultValue: '', description: 'Production context of the approved deployer identity'),
        string(name: 'PRODUCTION_IDENTITY', defaultValue: '', description: 'Name of the approved production deployer identity (owner decision D1)'),
        string(name: 'PROMPT_REGISTRY_URL', defaultValue: 'http://prompt-registry-agentregistry.ewallet-prompts.svc.cluster.local:12121', description: 'Prompt Management gate (agentregistry)'),
        string(name: 'STREAMING_SERVICE_ACCOUNT', defaultValue: '', description: 'Streaming campaign identity in ewallet-ci'),
        string(name: 'FEAST_SNAPSHOT_ID', defaultValue: '', description: 'Streaming campaign Feast snapshot id'),
        string(name: 'FEATURE_RUN_ID', defaultValue: '', description: 'Streaming campaign feature run id')
      ])
    ])
  }
  for (String required : ['EXPECTED_GIT_SHA', 'TARGET_BRANCH', 'INTENTIONAL_GATE_FAILURE', 'DEPLOY_APPROVED']) {
    if (!params.containsKey(required)) {
      error("Job parameter ${required} is not defined; seed it in Job DSL or pass declareParameters: true")
    }
  }

  String evidenceDir = "evidence/ci/${env.BUILD_TAG ?: 'unknown-build'}"
  String ciStatus = 'NOT_RUN'
  def userCauses = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
  String triggeredBy = userCauses ? (userCauses[0].userId ?: '') : ''
  timeout(time: 90, unit: 'MINUTES') {
    try {
      stage('Checkout and provenance') {
        withEnv(["TRIGGERED_BY=${triggeredBy}"]) {
          sh """#!/usr/bin/env bash
            set -euo pipefail
            test -n "$EXPECTED_GIT_SHA"
            test "\$(git rev-parse HEAD)" = "$EXPECTED_GIT_SHA"
            mkdir -p '${evidenceDir}'
            git status --short --branch > '${evidenceDir}/git-status.txt'
            printf 'build_url=%s\\nbuild_tag=%s\\ngit_sha=%s\\ntriggered_by=%s\\n' "\${BUILD_URL:-}" "\${BUILD_TAG:-}" "$EXPECTED_GIT_SHA" "\$TRIGGERED_BY" \\
              > '${evidenceDir}/jenkins-build.txt'
          """
        }
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
        try {
          sh """#!/usr/bin/env bash
            set -euo pipefail
            # Pinned, checksum-verified CLI tools (jenkins/ci-tools.json); fails closed on mismatch.
            tools_path="\$(python3 -m scripts.ci.provision_tools \
              --manifest jenkins/ci-tools.json \
              --cache-dir "\$HOME/.cache/ewallet-ci-tools" \
              --provenance '${evidenceDir}/ci-tools.json')"
            export PATH="\$tools_path:\$PATH"
            python3 -m scripts.ci.run_ci \
              --root "$WORKSPACE" \
              --scope '${evidenceDir}/scope.json' \
              --evidence-dir '${evidenceDir}' \
              ${profileArgument}
          """
        } finally {
          // PASS, FAILED or NOT_APPLICABLE, as decided by scripts/ci/run_ci.py (ci-verdict.json).
          ciStatus = sh(returnStdout: true, script: """#!/usr/bin/env bash
            python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' '${evidenceDir}/ci-verdict.json' 2>/dev/null || echo MISSING
          """).trim()
          currentBuild.description = "CI ${ciStatus}"
        }
      }

      if (params.INTENTIONAL_GATE_FAILURE) {
        stage('Intentional negative contract gate') {
          sh """#!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' 'intentional_negative_gate=blocked_before_release' > '${evidenceDir}/negative-gate.txt'
            exit 42
          """
        }
      }

      if (params.DEPLOY_APPROVED) {
        if (ciStatus != 'PASS') {
          error("Release refused: CI status is ${ciStatus}; a release needs a PASS with executed tests")
        }
        stage('Controlled release gate') {
          String approvedBy = input(
            message: "Approve the rehearsal release of ${params.EXPECTED_GIT_SHA} (${params.RELEASE_BRANCH})?",
            ok: 'Approve release',
            submitterParameter: 'APPROVED_BY'
          )
          env.RELEASE_APPROVED_BY = approvedBy
        }

        stage('Verify release branch') {
          withEnv(["RELEASE_REF=${params.RELEASE_BRANCH ?: ''}"]) {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              [[ "\$RELEASE_REF" =~ ^release/[A-Za-z0-9._/-]+\$ ]]
              test "\$(git rev-parse "refs/remotes/origin/\$RELEASE_REF^{commit}")" = "$EXPECTED_GIT_SHA"
              printf '%s %s\n' "\$RELEASE_REF" "$EXPECTED_GIT_SHA" > '${evidenceDir}/release-branch.txt'
            """
          }
        }

        String imageManifest = params.IMAGE_MANIFEST ?: "${evidenceDir}/image-manifest.json"
        stage('Plan release units') {
          withEnv(["RELEASE_PROFILE=${profile}", "RELEASE_BRANCH_REF=${params.RELEASE_BRANCH ?: ''}", "TRIGGERED_BY=${triggeredBy}"]) {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              test -n "$KUBE_CONTEXT"
              python3 -m scripts.ci.release_request \\
                --scope '${evidenceDir}/scope.json' --profile "\$RELEASE_PROFILE" \\
                --approved-by "\$RELEASE_APPROVED_BY" --triggered-by "\$TRIGGERED_BY" \\
                --branch "\$RELEASE_BRANCH_REF" --git-sha "$EXPECTED_GIT_SHA" \\
                --environment "$ENVIRONMENT" --kube-context "$KUBE_CONTEXT" \\
                --image-manifest '${imageManifest}' \\
                --evidence-dir '${evidenceDir}/release' --output '${evidenceDir}/release-request.json'
              python3 -m scripts.ci.release_policy --root "$WORKSPACE" images \\
                --request '${evidenceDir}/release-request.json' --output '${evidenceDir}/release-images.json'
            """
          }
        }

        if (!params.IMAGE_MANIFEST) {
          stage('Build or reuse images (Cloud Build)') {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              python3 -m scripts.ci.cloud_build \
                --root "$WORKSPACE" \
                --git-sha "$EXPECTED_GIT_SHA" \
                --images-file '${evidenceDir}/release-images.json' \
                --registry-lookup '${params.REGISTRY_LOOKUP ?: 'required'}' \
                --evidence-dir '${evidenceDir}/build' \
                --output '${imageManifest}'
            """
          }
        }

        stage('Scan images') {
          sh """#!/usr/bin/env bash
            set -euo pipefail
            python3 -m scripts.ci.image_scan --image-manifest '${imageManifest}' --output '${evidenceDir}/image-scan.json'
          """
        }

        stage('Rehearse release units') {
          // Only the units of this release read these: the Prompt Management gate the registry URL,
          // the streaming campaign its dedicated identity and its owner-supplied campaign ids.
          withEnv([
            "PROMPT_REGISTRY_URL=${params.PROMPT_REGISTRY_URL ?: ''}",
            "STREAMING_SERVICE_ACCOUNT=${params.STREAMING_SERVICE_ACCOUNT ?: ''}",
            "FEAST_SNAPSHOT_ID=${params.FEAST_SNAPSHOT_ID ?: ''}",
            "FEATURE_RUN_ID=${params.FEATURE_RUN_ID ?: ''}"
          ]) {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              python3 -m scripts.ci.release_policy --root "$WORKSPACE" build \
                --request '${evidenceDir}/release-request.json' \
                --image-manifest '${imageManifest}' \
                --output '${evidenceDir}/release-plan.json'
              scripts/ci/deploy_release.sh --plan '${evidenceDir}/release-plan.json' --execute
            """
          }
        }

        if (params.PROMOTE_TO_PRODUCTION) {
          stage('Production approval') {
            String productionApprover = input(
              message: "Promote ${params.EXPECTED_GIT_SHA} (${params.RELEASE_BRANCH}) to PRODUCTION with ${params.PRODUCTION_IDENTITY ?: '(no identity)'}?",
              ok: 'Promote to production',
              submitterParameter: 'APPROVED_BY'
            )
            if (!productionApprover || productionApprover == triggeredBy) {
              error('Production approval must come from someone other than the person who triggered the build')
            }
            env.PRODUCTION_APPROVED_BY = productionApprover
          }

          stage('Promote to production') {
            withEnv(["RELEASE_PROFILE=${profile}", "RELEASE_BRANCH_REF=${params.RELEASE_BRANCH ?: ''}", "TRIGGERED_BY=${triggeredBy}"]) {
              sh """#!/usr/bin/env bash
                set -euo pipefail
                test -n "$PRODUCTION_KUBE_CONTEXT"
                python3 -m scripts.ci.release_request \\
                  --scope '${evidenceDir}/scope.json' --profile "\$RELEASE_PROFILE" \\
                  --approved-by "\$PRODUCTION_APPROVED_BY" --triggered-by "\$TRIGGERED_BY" \\
                  --stage production --production-identity "$PRODUCTION_IDENTITY" \\
                  --branch "\$RELEASE_BRANCH_REF" --git-sha "$EXPECTED_GIT_SHA" \\
                  --environment "$ENVIRONMENT" --kube-context "$PRODUCTION_KUBE_CONTEXT" \\
                  --image-manifest '${imageManifest}' \\
                  --evidence-dir '${evidenceDir}/production' --output '${evidenceDir}/production-request.json'
                python3 -m scripts.ci.release_policy --root "$WORKSPACE" build \\
                  --request '${evidenceDir}/production-request.json' --image-manifest '${imageManifest}' \\
                  --output '${evidenceDir}/production-plan.json'
                PROMOTION_APPROVED=yes scripts/ci/deploy_release.sh --plan '${evidenceDir}/production-plan.json' \\
                  --execute --production-context "$PRODUCTION_KUBE_CONTEXT"
              """
            }
          }
        }
      }
    } finally {
      // Missing JUnit results are tolerated only when CI decided nothing needed testing.
      junit allowEmptyResults: (ciStatus == 'NOT_APPLICABLE'), testResults: "${evidenceDir}/**/junit*.xml"
      archiveArtifacts artifacts: "${evidenceDir}/**", allowEmptyArchive: false, fingerprint: true
    }
  }
}

return this
