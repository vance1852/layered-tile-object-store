# 分认领区底图包的稀疏版本存储

存储联调使用合成设备、时间轴和原始字节。设备记录由各客户端及介质的输入组成，每份记录可独立使用。

## 业务协议

瓦片以整数 `z,x,y` 表示。左上为坐标原点，从父级到子级两个方向均将整数放大一倍再加 0 或 1。对象正文在资料中表示为小像素栅格，宽和高都是 4，按从左上开始逐行存放，每个像素一个 ASCII 字节。低级对象的四个 2×2 象限分别覆盖其四个孩子；需要投影到孩子时，每个像素覆盖孩子的 2×2 小方格。服务读取时应能分辨各子区实际来源。
`claims` 的 `bounds` 是同一 `z` 上半开矩形，顺序为 x 起点、y 起点、x 终点、y 终点。更深瓦片按同一地理覆盖判定，较粗瓦片只有其认领范围内相应区域可产生影响。 `priority` 较大表示优先。`tile` 的 `kind` 为 `content`（正文实际存在）、`empty`（明确压住下层）或 `expected`（已声明应有正文但未到）；未列的区域没有声明。 `pickup` 的 `until` 是领用期限。

## Python 瓦片存储服务

仓库新增了标准库实现的 `tile_service` 包，不依赖第三方包：

```sh
python3 -m unittest -v
python3 -m tile_service replay fixtures/partial-claim-and-pinned-package --summary
python3 -m tile_service serve --fixture fixtures/partial-claim-and-pinned-package --port 8080
```

也可把资料目录初始化为持久服务：

```sh
python3 -m tile_service serve --data .tile-data --port 8080
```

### 核心模型

- `TileService.install_policy()`：装入 `claims` 与 `capacity_bytes`。
- `add_blob()`：按 SHA-256 内容寻址；相同底图在多个逻辑/图层版本中只存一份物理对象。
- `submit_update(layer, actor, base, tiles, revision=..., area=..., removed=...)`：
  以 `base` 图层修订做乐观并发检查，生成新的图层修订和全局逻辑版本。
- `resolve(z, x, y, version=None)` / `GET /explain`：返回逐像素来源、优先级链、
  是否投影/降一级替代、显式空白阻断原因和必需对象缺失状态。
- `tile_response()` / `GET /tiles/z/x/y`：
  - `200`：来源明确的瓦片或多源合成瓦片；
  - `203`：可使用但已标明来源的低优先级/跨比例替代或降级结果；
  - `204`：整块为显式空白，明确阻止继承；
  - `404`：所有图层均无声明；
  - `503`：`expected` 或内容对象缺失，必需对象暂不可用；`strict=1` 时即使存在最佳替代也返回 503。
- `create_pickup(id, extent=[z,x,y], levels=[...])`：把领用固定到当前逻辑版本，
  并立即持有该版本中所有可能来源对象的引用。
- `fetch_pickup_tile()` / `resume_pickup()`：拷贝单块或继续所有未完成/暂不可用项。
- `pickup_manifest()` / `GET /pickups/{id}`：清单逐项区分
  `content`、`alternative`、`composite`、`empty`、`pending`、`unavailable`。
- `build_package_zip()` / `GET /pickups/{id}/package.zip`：导出含
  `manifest.json`、可直接使用的 `tiles/` 和原始来源对象 `objects/` 的离线图包。
- `reference_report()` / `GET /usage`：列出物理对象被哪些图层修订、新旧领用包、
  仍支持的比例层级和活动下载引用；`withdrawn_or_superseded_pickup_holds`
  直接回答“哪个领用使已撤下的图层仍需保留”。
- `gc()` 与 `reclaim_layer_revision()`：只回收通过引用审计的对象/修订。
- `enable_layer(..., planned_bytes=...)` / `add_blob()` 只按
  `capacity_bytes - used_bytes` 的真实剩余空间准入；`projected_reclaimable_bytes`
  仅作维护提示，不会提前算作可用空间。必须先审计并实际 GC，再启用新图层。

### HTTP 示例

```sh
curl -s 'http://127.0.0.1:8080/explain?z=2&x=1&y=1' | python3 -m json.tool
curl -i 'http://127.0.0.1:8080/tiles/2/1/0?format=json'
curl -s -X POST http://127.0.0.1:8080/pickups/p9/fetch \
  -H 'Content-Type: application/json' \
  -d '{"tile":[2,1,0]}'
curl -OJ http://127.0.0.1:8080/pickups/p9/package.zip
```

兼容入口包括 `app.py`、`server.py`、`tile_store.py` 和 `tile_storage.py`；
测试可直接 `from tile_service import TileService`，也可使用 `TileStore` 别名。

## 本地设备资料

运行环境为 Python 3.11.2，使用标准库。设备资料端默认仅在 127.0.0.1:8090 运行，使用 --port 可另设端口。

~~~sh
python3 device_fixture.py fixtures/partial-claim-and-pinned-package
~~~

GET /fixture/events 返回设备参数、字节目录和按到达顺序记录的输入。同一时刻按文件先后排列。GET /fixture/objects/文件名 取原始字节。GET /fixture/journal 查看资料端已收到的设备请求，每个请求记录实际正文长度和摘要。

events.json 中的 packets 是可重放的设备响应。资料端按实际 HTTP 方法、路径和完整原始请求正文匹配第一条尚未使用的记录。没有匹配时返回 404。lag_ms 是回包延迟，drop 代表设备收到请求后直接断开连接，接收方需要以其他报告确认结果。没有 HTTP 交互的设备和客户端动作直接取 events。重新启动资料端会回到记录开头。

在项目根目录运行 python3 -m unittest -v 检查事件、对应字节、设备响应及接收日志。
