FROM php:8.3-cli-alpine

WORKDIR /app

COPY index.php .

EXPOSE 8000

VOLUME ["/data"]

CMD ["php", "-S", "0.0.0.0:8000", "/app/index.php"]
