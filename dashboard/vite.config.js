import { defineConfig } from 'vite'
import { readdirSync } from 'fs'
import { join } from 'path'

function scanRunsPlugin() {
  return {
    name: 'scan-runs',
    configureServer(server) {
      server.middlewares.use('/runs-list', (req, res) => {
        const runsDir = join(process.cwd(), 'runs')
        const allFiles = []
        try {
          const dirs = readdirSync(runsDir, { withFileTypes: true })
          for (const dir of dirs) {
            if (dir.isDirectory()) {
              const subDir = join(runsDir, dir.name)
              const files = readdirSync(subDir).filter(f => f.endsWith('.json'))
              for (const file of files) {
                allFiles.push(`${dir.name}/${file}`)
              }
            }
          }
        } catch (e) {
          // runs dir not found
        }
        res.setHeader('Content-Type', 'application/json')
        res.end(JSON.stringify(allFiles))
      })
    }
  }
}

export default defineConfig({
  root: '.',
  plugins: [scanRunsPlugin()],
  server: {
    port: 3000
  }
})