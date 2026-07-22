export interface ProductionAssets {
  stylesheets: string[]
  scripts: string[]
}

export function extractProductionAssets(html: string, documentUrl: URL): ProductionAssets
export function verifyProductionAssets(baseUrl: string | URL): Promise<ProductionAssets>
