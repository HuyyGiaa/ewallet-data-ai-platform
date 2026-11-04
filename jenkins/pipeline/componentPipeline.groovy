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
        string(name: 'RELEASE_BRANCH', defaultValue: '', description: 'release/<name> ref that must resolve to EXPECTED_GIT_SHA before any release'),
        booleanParam(name: 'INTENTIONAL_GATE_FAILURE', defaultValue: false, description: 'Runtime proof only: fail before every release action'),
        booleanParam(name: 'DEPLOY_APPROVED', defaultValue: false, description: 'Explicit request to enter the separately approved release gate'),
        choice(name: 'ENVIRONMENT', choices: ['dev', 'gcp', 'gke-cpu'], description: 'Validated release target'),
        string(name: 'KUBE_CONTEXT', defaultValue: '', description: 'Explicit context; the kubeconfig default is never used'),
        string(name: 'IMAGE_MANIFEST', defaultValue: '', description: 'Reviewed immutable image manifest; empty builds images with Cloud Build')
      ])
    ])
  }
  for (String required : ['EXPECTED_GIT_SHA', 'TARGET_BRANCH', 'INTENTIONAL_GATE_FAILURE', 'DEPLOY_APPROVED']) {
    if (!params.containsKey(required)) {
      error("Job parameter ${required} is not defined; seed it in Job DSL or pass declareParameters: true")
    }
  }

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
        stage('Controlled release gate') {
          String approvedBy = input(
            message: "Approve immutable release of ${params.EXPECTED_GIT_SHA} to ${params.ENVIRONMENT}?",
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

        String imageManifest = params.IMAGE_MANIFEST ?: ''
        if (!imageManifest) {
          stage('Build and publish images (Cloud Build)') {
            imageManifest = "${evidenceDir}/image-manifest.json"
            sh """#!/usr/bin/env bash
              set -euo pipefail
              python3 -m scripts.ci.cloud_build \
                --root "$WORKSPACE" \
                --git-sha "$EXPECTED_GIT_SHA" \
                --scope '${evidenceDir}/scope.json' \
                --profile '${profile}' \
                --evidence-dir '${evidenceDir}/build' \
                --output '${imageManifest}'
            """
          }
        }

        stage('Deploy and verify') {
          withEnv([
            "RELEASE_PROFILE=${profile}",
            "RELEASE_BRANCH_REF=${params.RELEASE_BRANCH ?: ''}",
            "RELEASE_IMAGE_MANIFEST=${imageManifest}"
          ]) {
            sh """#!/usr/bin/env bash
              set -euo pipefail
              test -n "$KUBE_CONTEXT"
              test -f "\$RELEASE_IMAGE_MANIFEST"
              python3 -m scripts.ci.release_request \\
                --scope '${evidenceDir}/scope.json' \\
                --profile "\$RELEASE_PROFILE" \\
                --approved-by "\$RELEASE_APPROVED_BY" \\
                --branch "\$RELEASE_BRANCH_REF" \\
                --git-sha "$EXPECTED_GIT_SHA" \\
                --environment "$ENVIRONMENT" \\
                --kube-context "$KUBE_CONTEXT" \\
                --image-manifest "\$RELEASE_IMAGE_MANIFEST" \\
                --evidence-dir '${evidenceDir}/release' \\
                --output '${evidenceDir}/release-request.json'
              python3 -m scripts.ci.release_policy \
                --root "$WORKSPACE" \
                build \
                --request '${evidenceDir}/release-request.json' \
                --image-manifest "\$RELEASE_IMAGE_MANIFEST" \
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
