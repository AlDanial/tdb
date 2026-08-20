class HomeController < ApplicationController
  def index
    render plain: "hello from the bundled Rails app"
  end
end