FROM php:8.2-fpm

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    unzip \
    libzip-dev \
    libpng-dev \
    libonig-dev \
    libxml2-dev \
    libicu-dev \
    libjpeg-dev \
    libfreetype6-dev \
    libmemcached-dev \
    zlib1g-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install \
        pdo_mysql \
        mysqli \
        zip \
        intl \
        gd \
        opcache

# Install smtp for sending mail
RUN apt-get update && apt-get install -y msmtp msmtp-mta

COPY msmtprc /etc/msmtprc
RUN chmod 600 /etc/msmtprc

# Install memcached extension
RUN pecl install memcached && docker-php-ext-enable memcached

# Install Xdebug (optional but recommended for PhpStorm)
RUN pecl install xdebug && docker-php-ext-enable xdebug

# Install Composer
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

# include PHP.ini
COPY php.ini /usr/local/etc/php/php.ini

# Set working directory
WORKDIR /var/www/html

# Permissions fix (important for WP)
RUN chown -R www-data:www-data /var/www