# 河流水质站日 CSV 发布服务

面向科研核验的不可改原件发布 API：原件原字节存共享卷，SQLite 只存目录与摘要，
以 SHA-256 固定深度 Merkle 树出具检查点、单件纳入证明与检查点追加证明，
并提供完全离线的验证函数/CLI。两个实例挂载同一卷并发发布，不分叉、不覆盖。

新增 **pH × 电导率统计包**：按发布序号读取 UTF-8 CSV 并核对原件摘要，
只保留充分统计量（不存逐行读数），统计包按包号持久化、跨实例与重启可查，
可反复合并（同一发布序号只计一次），合并结果与去重后直接统计逐位一致。

## 1. 目录结构

```
app/
  config.py     运行配置（DATA_DIR、1 MiB、128 件、树深度 7）
  storage.py    原件字节存储：内容寻址 + 临时文件 fsync + 原子改名
  db.py         SQLite 持久层：WAL、BEGIN IMMEDIATE 串行化、启动自检、统计包存取
  merkle.py     Merkle 树纯函数：根、纳入证明、追加证明、离线验证
  stats.py      统计包纯函数：CSV 解析校验、充分统计量、Pearson 系数
  verify.py     离线核验函数与 CLI（python -m app.verify ...）
  schemas.py    HTTP 响应模型
  main.py       FastAPI 路由（阻塞调用均走线程池）
tests/          48 项 pytest 用例（算法、API、统计包、故障路径）
scripts/
  demo.sh                 HTTP 端到端演示（含制包与合并）
  dual_instance_check.py  双进程共卷并发/故障转移集成检查
Dockerfile
docker-compose.yml        两个实例共卷，端口 8001/8002
```

## 2. 启动

### Docker Compose（交付目标形态）

```bash
docker compose up --build
# 实例 A：http://localhost:8001/docs
# 实例 B：http://localhost:8002/docs
```

两实例镜像相同、挂载同一命名卷 `shared-data`（`/data`），
`MAX_FILE_BYTES=1048576`、`MAX_RELEASES=128`（对应 Merkle 深度 7，128 个叶子槽）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
# 打开 http://localhost:8000/docs
```

## 3. API 一览（/docs 可交互演示）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/releases` | 表单 `station_id`、`sample_date`(YYYY-MM-DD)、`order_no`、`file`；同单同输入重试复用（`reused=true`），异输入 409，超 1 MiB 返回 413，超 128 件返回 409，写盘失败返回 507 |
| GET | `/releases` | 全部发布目录记录（不含字节） |
| GET | `/releases/{station}/{date}/{order}` | 单条目录记录 |
| GET | `/releases/{station}/{date}/{order}/download` | 下载已提交原件，文件名带站日单号 |
| GET | `/releases/{station}/{date}/{order}/inclusion-proof` | 单件纳入证明（恒 7 步路径） |
| GET | `/checkpoints` | 全部检查点（含 0 号空树检查点） |
| GET | `/checkpoints/latest` | 最新检查点（条数 + Merkle 根） |
| GET | `/checkpoints/{seq}` | 任意旧检查点 |
| GET | `/checkpoints/{old_seq}/append-proof` | 旧检查点 → 最新检查点的追加证明 |
| POST | `/stat-packages` | 制包：JSON `{"release_ids": [1,2]}`；按发布序号读 UTF-8 CSV、核对 SHA-256，汇总 pH×电导率充分统计量；重复序号自动去重 |
| POST | `/stat-packages/merge` | 合并：JSON `{"package_ids": [1,2]}`；跨包同一发布序号只计一次，不重读原件，合出的包可继续合并 |
| GET | `/stat-packages` | 全部统计包（包号、所含发布序号、有效行数、Pearson 系数） |
| GET | `/stat-packages/{package_id}` | 按包号查询统计包 |
| GET | `/health` | 启动自检结果（缺件/摘要不符时 `degraded`，并拒发证明与下载） |

### 统计包语义（制包与合并）

* **CSV 格式**：UTF-8（容忍 BOM），表头必须含 `ph` 与 `conductivity_us_cm` 两列，
  其余列忽略；两列单元格均可留空，仅两列都有值的行参与统计。
* **取值校验**：非空值必须是不超过六位小数的十进制数（如 `7`、`7.123456`、`.5`），
  pH 须在 [0, 14]，电导率须在 [0, 1000000]；任一非空单元格不合规即整次制包失败（422）。
* **精确汇总**：测量值按 1e6 定标为整数，逐发布序号保存充分统计量
  `(n, Σx, Σy, Σx², Σy², Σxy)`（大整数以十进制字符串落库），不保留逐行读数；
  Pearson 系数由整数和现算，样本不足两行或任一列恒定时为 `null`。
* **合并**：按发布序号取并集（同一序号只计一次；异单同字节因序号不同分别计入），
  统计量逐项相加，只读库不重读原件；因全程整数精确求和，
  合并结果与“去重后对原件直接统计”逐位一致。
* **失败原子性**：缺件/摘要不符（503）、序号不存在（404）、格式错误（422）
  均整次失败、不留半个包；已有包不受影响，仍可查询、可合并。
* **持久性**：统计包存于共享卷 SQLite，包号 1 起稠密，跨实例与重启后均可查询。

## 4. 核验模型（科研组离线使用）

### 叶子与树

* 叶子载荷为带版本号的规范化 JSON（键排序、无空白）：
  `{v, station_id, sample_date, order_no, blob_sha256, size}`，
  叶子哈希 = `SHA256(0x00 || 载荷)`；内部节点 = `SHA256(0x01 || left || right)`，域分隔防二阶碰撞。
* 固定深度 7 的完整二叉树，未用槽位以公开常量 `EMPTY_LEAF` 占位：
  追加只改变新叶子到根的路径，旧证明不会因树“长高”失效。
* **纳入证明**：7 个兄弟哈希（O(log n)），每步带 `bit` 与下标逐位绑定，防重放到其他下标。
* **追加证明**：把旧前缀与新增区间分别平铺为对齐的完美子树，条目数 ≤ 2×深度（实测 128 件规模最多 8 条）。

### 离线核验（无需服务端、无需网络）

```bash
# 单件：凭留存的纳入证明 + 待验原件，核验站日归属与检查点纳入
python -m app.verify inclusion --proof inclusion.json --file 原件.csv

# 历史前缀：凭两个留存检查点 + 追加证明，核验仅追加关系
python -m app.verify append --proof append.json \
  --old-checkpoint cp-old.json --new-checkpoint cp-new.json
```

也可直接导入纯函数：`app.verify.verify_release_bytes`、`app.verify.verify_append_proof`。
篡改原件、替换历史条目、伪造检查点根、证明重放到其他下标均会被拒绝（退出码 1）。

## 5. 崩溃安全与并发不变量

1. **先落盘后入库**：原件写临时文件 → `fsync` → `os.replace` 原子改名 → `fsync` 目录，
   成功后才开数据库事务；写盘失败无库记录、临时文件清理、可原样重试。
2. **串行化发布**：每个发布事务 `BEGIN IMMEDIATE`，双实例写操作被 SQLite 锁串行化，
   后到者等待后重检唯一键 `(station_id, sample_date, order_no)`；无 UPDATE/DELETE 路径。
3. **只增不改**：同键同字节复用、同键异字节 409；检查点与记录均只追加。
4. **启动自检**：重算全部叶子与根，逐件校验卷上原件摘要并检查序号稠密、检查点连续；
   发现缺件即 `degraded`，拒绝发布/证明/下载，绝不输出缺件记录。
5. **重启有效**：历史检查点根由固定深度树确定，重启后逐点复算一致，留存证明持续有效。
6. 阻塞式磁盘/DB 调用均在任意线程池执行，锁等待不阻塞事件循环；
   写锁冲突在 `busy_timeout` 之外另有 3 次有限重试。

## 6. 测试结果（本环境实测）

* `pytest tests/`：**48 项全部通过**
  - Merkle：根稳定性、纳入证明、全部前缀追加证明、篡改/重放/重叠/缺口拒绝、平铺覆盖性质；
  - API：发布、幂等复用、409 冲突、413/422 限额与校验、128 上限、下载字节一致、两类证明与离线核验；
  - 统计包：空单元格行不计入、六位小数/取值范围/缺列/非 UTF-8/空文件/引号未闭合拒绝、
    Pearson 空值规则（不足两行、任一列恒定）与完全线性 ±1、请求内序号去重、
    合并跨包去重且与一次性直接制包逐位一致、异单同字节分别计入、合并包再合并、
    未知序号/包号 404、格式错误 422 且不留半个包、缺件 503 后已有包仍可查可合并、
    换数据库连接（等价重启/另一实例）后包可查；
  - 故障：写盘失败不留记录且可重试、缺件自检与 503 拒服、孤立临时/原件无害、已引用原件被改可检出、重启后检查点可复算。
* `scripts/dual_instance_check.py`（两个独立 uvicorn 进程共卷）：**22 项全部通过**，
  覆盖并发同单（一写一复用）、10 件并发异单序号 1..11 稠密、跨实例取件/检查点/证明、
  第三进程挂同卷的故障转移视角与继续追加，以及统计包跨实例制包/查询/合并
  （A 制包 B 可见，B 合并 A 可查，合并 Pearson 与直接统计一致）。
* 手工冒烟（默认 1 MiB / 128 件 / 深度 7）：1 MiB+1 返回 413、恰 1 MiB 成功、
  发满 128 件后第 129 件 409、检查点 0..128 连续、离线 CLI 核验通过、篡改原件被拒。
* `scripts/demo.sh` 对本机实例端到端跑通：发布 → 幂等 → 冲突 → 取件 → 证明 →
  离线核验 → 篡改拒绝 → 制包（空 pH 行不计入）→ 合并去重 → 按包号查询。
* 证明规模实测：纳入证明恒 7 步；追加证明最大条目随条数 1→2→3→…→8（对数增长）。

## 7. 剩余工作与已知限制（据实说明）

1. **本环境无 Docker 守护进程**，`docker compose` 未实际起容器验证；已用双 uvicorn
   进程共享同一 `DATA_DIR` 做等价并发/故障转移测试。Dockerfile 与 compose 配置已交付，
   需在具备 Docker 的环境执行一次 `docker compose up --build` 复核。
2. 沙箱以 seccomp 禁止 `kill` 系统调用，集成脚本无法终止演示进程，改为随机端口 +
   每次独立临时目录；脚本中的“重启”以第三进程挂同卷模拟（故障转移语义等价）。
3. 128 件上限在实现上是固定深度树的叶子槽位数（2 的幂，可经 `MAX_RELEASES` 调整）；
   非 2 的幂配置会在启动时直接报错。
4. 未引入鉴权/传输加密/TLS，定位为内网或网关后的发布服务；对外暴露需前置网关。
5. 共享卷自身的备份与异地容灾未包含在本服务内（卷快照/备份应由基础设施层提供）；
   服务能检出卷上缺件与篡改，但不会自动恢复字节。
6. 原件类型未强制 CSV 内容嗅探，仅做大小与空文件校验，避免对业务 CSV 方言做过强假设；
   仅统计包制包时才按上述格式规则解析 `ph`/`conductivity_us_cm` 两列。
7. 统计包为派生数据：启动自检不复核包内统计量（原件不可改、制包时已核摘要，
   包内容确定）；若库中统计包表被直接篡改，服务不另做检测——篡改面由卷级备份/权限控制。
8. 统计包接口的数值解析有意从严：不接受科学计数法、符号前缀与超过六位的小数，
   空值仅指空单元格（含纯空白），`NA`/`null` 等占位写法视为格式错误。
