# 开发阶段
FROM node:24-alpine AS development
WORKDIR /app
ENV TZ=Asia/Shanghai

# 固定包管理器版本，确保本地、CI 与镜像使用同一份锁文件语义
RUN npm install -g pnpm@10.11.0

# 复制 package.json 和 pnpm-lock.yaml
COPY ./web/package*.json ./
COPY ./web/pnpm-lock.yaml* ./
COPY ./web/pnpm-workspace.yaml ./

# 安装依赖
RUN pnpm install --frozen-lockfile --registry=https://registry.npmmirror.com

# 复制源代码
COPY ./web .

# 暴露端口
EXPOSE 5173

# 启动开发服务器的命令在 docker-compose 文件中定义

# 生产阶段
FROM node:24-alpine AS build-stage
WORKDIR /app

# 与开发镜像保持一致，避免生产构建使用不同的解析器版本
RUN npm install -g pnpm@10.11.0

# 复制依赖文件
COPY ./web/package*.json ./
COPY ./web/pnpm-lock.yaml* ./
COPY ./web/pnpm-workspace.yaml ./

# 安装依赖
RUN pnpm install --frozen-lockfile --registry=https://registry.npmmirror.com

# 复制源代码并构建
COPY ./web .
RUN pnpm run build

# 生产环境运行阶段
FROM nginx:alpine AS production
COPY --from=build-stage /app/dist /usr/share/nginx/html
COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/default.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
