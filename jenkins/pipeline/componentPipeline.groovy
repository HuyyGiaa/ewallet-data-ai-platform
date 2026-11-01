def run(Map configuration = [:]) {
  String profile = configuration.get('profile', 'platform')
  properties([
    disableConcurrentBuilds(),
    parameters([
      string(name: 'EXPECTED_GIT_SHA', defaultValue: '', description: 'Exact reviewed 40-character Git SHA'),
      string(name: 'TARGET_BRANCH', defaultValue: 'origin/main', description: 'Fetched target ref used only for merge-base change detection'),
      booleanParam(name: 'DEPLOY_APPROVED', defaultValue: false, description: 'Explicit request to enter the separately approved release gate'),
      choice(name: 'ENVIRONMENT', choices: ['dev', 'gcp', 'gke-cpu'], description: 'Validated release target'),
      string(name: 'KUBE_CONTEXT', defaultValue: '', description: 'Explicit context; the kubeconfig default is never used')
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
          error('Controlled CD dispatcher is fail-closed until its release policy is installed')
        }
      }
    } finally {
      junit allowEmptyResults: true, testResults: "${evidenceDir}/**/junit*.xml"
      archiveArtifacts artifacts: "${evidenceDir}/**", allowEmptyArchive: false, fingerprint: true
    }
  }
}

return this
