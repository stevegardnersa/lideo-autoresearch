import { defineConfig } from 'vite'
import { readdirSync, readFileSync, existsSync } from 'fs'
import { join, extname } from 'path'

function stripQuery(url) {
  const i = url.indexOf('?')
  return i >= 0 ? url.slice(0, i) : url
}

function serveStaticFile(rootDir, urlPath) {
  const safePath = stripQuery(urlPath).replace(/^\/+/, '')
  const filePath = join(rootDir, safePath)
  if (!existsSync(filePath)) return null
  try {
    const content = readFileSync(filePath, 'utf-8')
    const ext = extname(filePath)
    const mimeTypes = {
      '.json': 'application/json',
      '.jsonl': 'application/jsonl',
      '.txt': 'text/plain',
      '.csv': 'text/csv',
      '.tsv': 'text/tab-separated-values',
      '.md': 'text/markdown',
      '.html': 'text/html',
      '.css': 'text/css',
      '.js': 'application/javascript',
    }
    return { content, contentType: mimeTypes[ext] || 'application/octet-stream' }
  } catch {
    return null
  }
}

function scanRunsPlugin() {
  return {
    name: 'scan-runs',
    configureServer(server) {
      const runsDir = join(process.cwd(), 'runs')
      const dataDir = join(process.cwd(), 'data')

      server.middlewares.use((req, res, next) => {
        if (req.url === '/runs-list') {
          const allFiles = []
          try {
            const dirs = readdirSync(runsDir, { withFileTypes: true })
            for (const dir of dirs) {
              if (dir.isDirectory()) {
                const subDir = join(runsDir, dir.name)
                const files = readdirSync(subDir).filter(f => f.endsWith('.json') || f.endsWith('.jsonl'))
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
          return
        }

        if (req.url.startsWith('/runs/')) {
          const relativePath = req.url.slice('/runs/'.length)
          const result = serveStaticFile(runsDir, relativePath)
          if (!result) {
            res.statusCode = 404
            res.end('Not found')
            return
          }
          res.setHeader('Content-Type', result.contentType)
          res.end(result.content)
          return
        }

        if (req.url.startsWith('/data/')) {
          const relativePath = req.url.slice('/data/'.length)
          const result = serveStaticFile(dataDir, relativePath)
          if (!result) {
            res.statusCode = 404
            res.end('Not found')
            return
          }
          res.setHeader('Content-Type', result.contentType)
          res.end(result.content)
          return
        }

        next()
      })
    }
  }
}

export default defineConfig({
  root: '.',
  plugins: [scanRunsPlugin()],
  server: {
    port: 3001
  }
})