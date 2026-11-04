node {
  checkout scm
  def componentPipeline = load 'jenkins/pipeline/componentPipeline.groovy'
  componentPipeline.run(profile: 'platform')
}
